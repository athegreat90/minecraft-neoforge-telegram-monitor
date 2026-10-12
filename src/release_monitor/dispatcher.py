"""Deliver queued notifications to Telegram and record every outcome.

The outbox row is the source of truth. A row is claimed (``sending``) before the network call
and acknowledged straight after the 200, so the only window for a duplicate is a crash or lost
response between Telegram accepting a message and that acknowledgement. Multi-part messages
persist their progress after every part, so a retry never resends parts that were delivered.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from release_monitor.models import Notification, Source
from release_monitor.notifier.telegram import (
    AmbiguousDeliveryError,
    PermanentDeliveryError,
    RateLimited,
    TransientDeliveryError,
)
from release_monitor.storage.repository import Repository
from release_monitor.timeutil import utc_now

LOG = logging.getLogger(__name__)

MAX_NOTIFICATIONS_PER_RUN = 1000
_MAX_BACKOFF_EXPONENT = 20


class MessageSender(Protocol):
    def send_message(self, chat_id: str, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class DispatcherSettings:
    ambiguous_policy: str = "retry"
    min_interval: float = 1.1
    backoff_base: timedelta = timedelta(seconds=30)
    backoff_cap: timedelta = timedelta(hours=1)
    ambiguous_retry_delay: timedelta = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    sent: int = 0
    retried: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    """The outbox payload: the message parts and how many were already acknowledged."""

    parts: list[str]
    sent_parts: int = 0
    message_ids: list[int] = field(default_factory=list)

    def encode(self) -> str:
        return json.dumps(
            {"parts": self.parts, "sent_parts": self.sent_parts, "message_ids": self.message_ids}
        )

    @classmethod
    def decode(cls, text: str) -> NotificationPayload:
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError("invalid notification payload: not JSON") from None
        parts = data.get("parts") if isinstance(data, dict) else None
        if not (isinstance(parts, list) and parts and all(isinstance(p, str) for p in parts)):
            raise ValueError("invalid notification payload: 'parts' must be a list of strings")
        sent = data.get("sent_parts", 0)
        if not isinstance(sent, int) or not 0 <= sent <= len(parts):
            raise ValueError("invalid notification payload: bad 'sent_parts'")
        ids = data.get("message_ids", [])
        if not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
            raise ValueError("invalid notification payload: bad 'message_ids'")
        return cls(parts=parts, sent_parts=sent, message_ids=ids)


def encode_payload(parts: Sequence[str]) -> str:
    """The initial outbox payload for a message made of ``parts``."""
    return NotificationPayload(parts=list(parts)).encode()


class Dispatcher:
    """Sends due notifications one at a time, oldest first."""

    def __init__(
        self,
        repository: Repository,
        sender: MessageSender,
        chat_ids: Mapping[Source, str],
        *,
        settings: DispatcherSettings | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repo = repository
        self._sender = sender
        self._chat_ids = dict(chat_ids)
        self._settings = settings or DispatcherSettings()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_send: dict[str, float] = {}

    def recover_on_start(self) -> list[Notification]:
        """Resolve rows a previous run left in ``sending``.

        Only one instance can hold the database lock, so any such row is an interrupted send
        whose outcome is unknown; the configured policy decides between retry and skip.
        """
        return self._repo.recover_stale_sending(
            self._clock(), timedelta(0), self._settings.ambiguous_policy
        )

    def run_due(self) -> DispatchSummary:
        """Deliver every notification that is due now."""
        summary = DispatchSummary()
        for _ in range(MAX_NOTIFICATIONS_PER_RUN):
            notification = self._repo.claim_next_due(self._clock())
            if notification is None:
                break
            outcome = self._deliver(notification)
            summary = replace(summary, **{outcome: getattr(summary, outcome) + 1})
        return summary

    # -- internals ----------------------------------------------------------------------

    def _deliver(self, notification: Notification) -> str:
        label = f"{notification.source.value} {notification.version}"
        chat_id = self._chat_ids.get(notification.source)
        if chat_id is None:
            return self._fail(notification, label, "no_chat_configured", "no channel configured")
        try:
            payload = NotificationPayload.decode(notification.payload)
        except ValueError as exc:
            return self._fail(notification, label, "invalid_payload", str(exc))

        for index in range(payload.sent_parts, len(payload.parts)):
            self._pace(chat_id)
            try:
                message_id = self._sender.send_message(chat_id, payload.parts[index])
            except RateLimited as exc:
                delay = timedelta(seconds=max(1.0, exc.retry_after))
                return self._retry(notification, label, delay, "rate_limited", str(exc))
            except TransientDeliveryError as exc:
                delay = self._backoff(notification.attempts)
                return self._retry(notification, label, delay, "transient", str(exc))
            except PermanentDeliveryError as exc:
                return self._fail(notification, label, "permanent", str(exc))
            except AmbiguousDeliveryError as exc:
                return self._ambiguous(notification, label, str(exc))
            except Exception as exc:  # a bug must not stall the queue or lose the row
                LOG.error("unexpected %s while delivering %s", type(exc).__name__, label)
                delay = self._backoff(notification.attempts)
                return self._retry(notification, label, delay, "unexpected_error", None)
            self._last_send[chat_id] = self._monotonic()
            payload = replace(
                payload,
                sent_parts=index + 1,
                message_ids=[*payload.message_ids, message_id],
            )
            self._repo.update_payload(notification.id, payload.encode())

        first_id = payload.message_ids[0] if payload.message_ids else None
        self._repo.mark_sent(notification.id, first_id, self._clock())
        LOG.info("delivered %s (%d part(s))", label, len(payload.parts))
        return "sent"

    def _ambiguous(self, notification: Notification, label: str, detail: str) -> str:
        if self._settings.ambiguous_policy == "skip":
            LOG.warning("giving up on %s: delivery outcome unknown (%s)", label, detail)
            self._repo.mark_failed_permanent(notification.id, "ambiguous_skipped")
            return "failed"
        return self._retry(
            notification, label, self._settings.ambiguous_retry_delay, "ambiguous_retry", detail
        )

    def _retry(
        self,
        notification: Notification,
        label: str,
        delay: timedelta,
        error_class: str,
        detail: str | None,
    ) -> str:
        due = self._clock() + delay
        self._repo.mark_retry(notification.id, due, error_class)
        LOG.warning(
            "will retry %s in %ds (%s)%s",
            label,
            int(delay.total_seconds()),
            error_class,
            f": {detail}" if detail else "",
        )
        return "retried"

    def _fail(self, notification: Notification, label: str, error_class: str, detail: str) -> str:
        self._repo.mark_failed_permanent(notification.id, error_class)
        LOG.error("giving up on %s (%s): %s", label, error_class, detail)
        return "failed"

    def _backoff(self, attempts: int) -> timedelta:
        exponent = min(max(attempts - 1, 0), _MAX_BACKOFF_EXPONENT)
        return min(self._settings.backoff_cap, self._settings.backoff_base * (1 << exponent))

    def _pace(self, chat_id: str) -> None:
        """Keep at least ``min_interval`` seconds between messages to the same chat."""
        last = self._last_send.get(chat_id)
        if last is None:
            return
        wait = self._settings.min_interval - (self._monotonic() - last)
        if wait > 0:
            self._sleep(wait)
