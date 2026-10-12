"""Outbox dispatcher: delivery, retries, pacing and crash recovery."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from release_monitor.dispatcher import (
    Dispatcher,
    DispatcherSettings,
    DispatchSummary,
    NotificationPayload,
    encode_payload,
)
from release_monitor.models import NotificationState, Origin, ReleaseStatus, Source
from release_monitor.notifier.telegram import (
    AmbiguousDeliveryError,
    PermanentDeliveryError,
    RateLimited,
    TransientDeliveryError,
)
from release_monitor.storage import open_database
from release_monitor.storage.repository import InvalidStateError, Repository

from .helpers import T0, FakeClock, release

MC_CHAT = "@mc_test_channel"
NF_CHAT = "@nf_test_channel"
CHATS = {Source.MINECRAFT: MC_CHAT, Source.NEOFORGE: NF_CHAT}

Outcome = int | Exception


class FakeSender:
    """Records sends and replays scripted outcomes (default: succeed with an increasing id)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.script: list[Outcome] = []
        self._next_id = 100

    def send_message(self, chat_id: str, text: str) -> int:
        self.calls.append((chat_id, text))
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self._next_id += 1
        return self._next_id


class FakeTime:
    """Monotonic time that only moves when the dispatcher sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_database(tmp_path / "releases.sqlite3")) as connection:
        yield connection


@pytest.fixture
def repo(conn: sqlite3.Connection, clock: FakeClock) -> Repository:
    return Repository(conn, clock=clock)


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def make_dispatcher(
    repo: Repository,
    sender: FakeSender,
    clock: FakeClock,
    fake_time: FakeTime,
    **settings: Any,
) -> Dispatcher:
    return Dispatcher(
        repo,
        sender,
        CHATS,
        settings=DispatcherSettings(**settings),
        clock=clock,
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )


def queue(
    repo: Repository,
    version: str,
    *parts: str,
    source: Source = Source.NEOFORGE,
    due_in: timedelta | None = None,
) -> int:
    repo.add_discovered(release(version, source=source), ReleaseStatus.DEFERRED)
    return repo.enqueue(
        source,
        version,
        encode_payload(list(parts) or [f"message for {version}"]),
        Origin.LIVE,
        next_attempt_at=(T0 + due_in) if due_in else None,
    )


def state_of(repo: Repository, notification_id: int) -> NotificationState:
    notification = repo.get_notification(notification_id)
    assert notification is not None
    return notification.state


# -- happy path -------------------------------------------------------------------------


def test_a_due_notification_is_sent_to_the_channel_of_its_source(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    mc_id = queue(repo, "26.3", "minecraft text", source=Source.MINECRAFT)
    nf_id = queue(repo, "26.3.0.76", "neoforge text")
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    summary = dispatcher.run_due()

    assert summary == DispatchSummary(sent=2, retried=0, failed=0)
    assert (MC_CHAT, "minecraft text") in sender.calls
    assert (NF_CHAT, "neoforge text") in sender.calls
    for notification_id in (mc_id, nf_id):
        notification = repo.get_notification(notification_id)
        assert notification is not None
        assert notification.state is NotificationState.SENT
        assert notification.telegram_message_id is not None
        assert notification.sent_at == clock.now
    release_row = repo.get_release(Source.NEOFORGE, "26.3.0.76")
    assert release_row is not None
    assert release_row.status is ReleaseStatus.ANNOUNCED


def test_nothing_is_sent_twice_after_success(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    queue(repo, "26.3.0.76")
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    dispatcher.run_due()
    clock.advance(hours=1)
    dispatcher.run_due()
    dispatcher.run_due()

    assert len(sender.calls) == 1


def test_notifications_that_are_not_due_yet_wait(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76", due_in=timedelta(minutes=10))
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    assert dispatcher.run_due() == DispatchSummary(0, 0, 0)
    clock.advance(minutes=11)
    assert dispatcher.run_due().sent == 1
    assert state_of(repo, notification_id) is NotificationState.SENT


# -- multi-part resume --------------------------------------------------------------------


def test_a_failed_multi_part_message_resumes_without_resending_delivered_parts(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76", "part 1", "part 2", "part 3")
    sender.script = [501, TransientDeliveryError("boom")]
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    first = dispatcher.run_due()
    assert first == DispatchSummary(sent=0, retried=1, failed=0)
    pending = repo.get_notification(notification_id)
    assert pending is not None
    assert pending.state is NotificationState.PENDING
    assert NotificationPayload.decode(pending.payload).sent_parts == 1

    clock.advance(hours=2)
    second = dispatcher.run_due()

    assert second.sent == 1
    assert [text for _chat, text in sender.calls] == ["part 1", "part 2", "part 2", "part 3"]
    done = repo.get_notification(notification_id)
    assert done is not None
    assert done.state is NotificationState.SENT
    assert done.telegram_message_id == 501  # the first part's id


# -- retries --------------------------------------------------------------------------------


def test_rate_limits_schedule_the_retry_exactly_retry_after_seconds_out(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    sender.script = [RateLimited(42)]
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    summary = dispatcher.run_due()

    assert summary.retried == 1
    notification = repo.get_notification(notification_id)
    assert notification is not None
    assert notification.state is NotificationState.PENDING
    assert notification.next_attempt_at == clock.now + timedelta(seconds=42)
    assert notification.last_error_class == "rate_limited"

    assert dispatcher.run_due().sent == 0  # still waiting
    assert len(sender.calls) == 1
    clock.advance(seconds=42)
    assert dispatcher.run_due().sent == 1


def test_transient_failures_back_off_exponentially_up_to_the_cap(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    sender.script = [TransientDeliveryError("down")] * 12
    dispatcher = make_dispatcher(
        repo,
        sender,
        clock,
        fake_time,
        backoff_base=timedelta(seconds=30),
        backoff_cap=timedelta(hours=1),
    )

    delays: list[float] = []
    for _ in range(10):
        dispatcher.run_due()
        notification = repo.get_notification(notification_id)
        assert notification is not None
        delays.append((notification.next_attempt_at - clock.now).total_seconds())
        clock.now = notification.next_attempt_at

    assert delays[:5] == [30, 60, 120, 240, 480]
    assert delays[-1] == 3600
    assert max(delays) == 3600


def test_unexpected_errors_are_retried_and_do_not_stop_other_notifications(
    repo: Repository,
    sender: FakeSender,
    clock: FakeClock,
    fake_time: FakeTime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = queue(repo, "26.3.0.75-beta")
    fine = queue(repo, "26.3.0.76")
    sender.script = [RuntimeError("bug")]
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    with caplog.at_level(logging.ERROR):
        summary = dispatcher.run_due()

    assert summary == DispatchSummary(sent=1, retried=1, failed=0)
    broken_row = repo.get_notification(broken)
    assert broken_row is not None
    assert broken_row.last_error_class == "unexpected_error"
    assert state_of(repo, fine) is NotificationState.SENT
    assert "RuntimeError" in caplog.text


# -- permanent failures ----------------------------------------------------------------------


def test_a_permanent_failure_is_recorded_and_isolated(
    repo: Repository,
    sender: FakeSender,
    clock: FakeClock,
    fake_time: FakeTime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = queue(repo, "26.3.0.75-beta")
    good = queue(repo, "26.3.0.76")
    sender.script = [PermanentDeliveryError("chat not found: check the chat id")]
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    with caplog.at_level(logging.ERROR):
        summary = dispatcher.run_due()

    assert summary == DispatchSummary(sent=1, retried=0, failed=1)
    failed = repo.get_notification(bad)
    assert failed is not None
    assert failed.state is NotificationState.FAILED_PERMANENT
    assert failed.last_error_class == "permanent"
    assert state_of(repo, good) is NotificationState.SENT
    assert "chat not found" in caplog.text


def test_a_source_without_a_configured_chat_fails_permanently(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    dispatcher = Dispatcher(
        repo, sender, {Source.MINECRAFT: MC_CHAT}, clock=clock,
        monotonic=fake_time.monotonic, sleep=fake_time.sleep,
    )  # fmt: skip

    assert dispatcher.run_due().failed == 1
    assert state_of(repo, notification_id) is NotificationState.FAILED_PERMANENT
    assert sender.calls == []


def test_an_unreadable_payload_fails_permanently_without_sending(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    repo.add_discovered(release("26.3.0.76"), ReleaseStatus.DEFERRED)
    notification_id = repo.enqueue(Source.NEOFORGE, "26.3.0.76", "not json", Origin.LIVE)
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    assert dispatcher.run_due().failed == 1
    assert state_of(repo, notification_id) is NotificationState.FAILED_PERMANENT
    assert sender.calls == []


# -- ambiguous outcomes and crash recovery ----------------------------------------------------


def test_an_ambiguous_send_is_retried_under_the_retry_policy(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    sender.script = [AmbiguousDeliveryError("timed out after sending")]
    dispatcher = make_dispatcher(
        repo, sender, clock, fake_time, ambiguous_policy="retry",
        ambiguous_retry_delay=timedelta(seconds=90),
    )  # fmt: skip

    assert dispatcher.run_due().retried == 1
    notification = repo.get_notification(notification_id)
    assert notification is not None
    assert notification.state is NotificationState.PENDING
    assert notification.last_error_class == "ambiguous_retry"
    assert notification.next_attempt_at == clock.now + timedelta(seconds=90)


def test_an_ambiguous_send_is_given_up_under_the_skip_policy(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    sender.script = [AmbiguousDeliveryError("timed out after sending")]
    dispatcher = make_dispatcher(repo, sender, clock, fake_time, ambiguous_policy="skip")

    assert dispatcher.run_due().failed == 1
    notification = repo.get_notification(notification_id)
    assert notification is not None
    assert notification.state is NotificationState.FAILED_PERMANENT
    assert notification.last_error_class == "ambiguous_skipped"


def test_a_crash_while_sending_is_recovered_on_start_under_the_retry_policy(
    repo: Repository,
    sender: FakeSender,
    clock: FakeClock,
    fake_time: FakeTime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    claimed = repo.claim_next_due(clock.now)  # the process "dies" right after this
    assert claimed is not None
    assert state_of(repo, notification_id) is NotificationState.SENDING

    restarted = make_dispatcher(repo, sender, clock, fake_time, ambiguous_policy="retry")
    with caplog.at_level(logging.WARNING):
        recovered = restarted.recover_on_start()

    assert [item.id for item in recovered] == [notification_id]
    assert "ambiguous retry" in caplog.text
    assert restarted.run_due().sent == 1
    assert state_of(repo, notification_id) is NotificationState.SENT


def test_a_crash_while_sending_is_given_up_on_start_under_the_skip_policy(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    notification_id = queue(repo, "26.3.0.76")
    repo.claim_next_due(clock.now)

    restarted = make_dispatcher(repo, sender, clock, fake_time, ambiguous_policy="skip")
    restarted.recover_on_start()

    assert restarted.run_due() == DispatchSummary(0, 0, 0)
    notification = repo.get_notification(notification_id)
    assert notification is not None
    assert notification.state is NotificationState.FAILED_PERMANENT
    assert notification.last_error_class == "ambiguous_skipped"
    assert sender.calls == []


# -- pacing ---------------------------------------------------------------------------------------


def test_messages_to_the_same_chat_are_paced(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    queue(repo, "26.3.0.75-beta")
    queue(repo, "26.3.0.76")
    dispatcher = make_dispatcher(repo, sender, clock, fake_time, min_interval=1.1)

    dispatcher.run_due()

    assert len(sender.calls) == 2
    assert len(fake_time.sleeps) == 1
    assert fake_time.sleeps[0] == pytest.approx(1.1)


def test_messages_to_different_chats_are_not_delayed(
    repo: Repository, sender: FakeSender, clock: FakeClock, fake_time: FakeTime
) -> None:
    queue(repo, "26.3", source=Source.MINECRAFT)
    queue(repo, "26.3.0.76")
    dispatcher = make_dispatcher(repo, sender, clock, fake_time)

    dispatcher.run_due()

    assert len(sender.calls) == 2
    assert fake_time.sleeps == []


# -- repository support ----------------------------------------------------------------------


def test_update_payload_is_only_allowed_while_sending(repo: Repository, clock: FakeClock) -> None:
    notification_id = queue(repo, "26.3.0.76")
    with pytest.raises(InvalidStateError):
        repo.update_payload(notification_id, "{}")

    repo.claim_next_due(clock.now)
    repo.update_payload(notification_id, '{"parts": ["x"], "sent_parts": 0, "message_ids": []}')
    notification = repo.get_notification(notification_id)
    assert notification is not None
    assert notification.payload.startswith('{"parts"')


def test_payload_round_trip_and_validation() -> None:
    payload = NotificationPayload.decode(encode_payload(["a", "b"]))
    assert payload.parts == ["a", "b"]
    assert payload.sent_parts == 0
    assert payload.message_ids == []

    for bad in (
        "not json",
        "[]",
        '{"parts": []}',
        '{"parts": [1]}',
        '{"parts": ["a"], "sent_parts": 5}',
    ):
        with pytest.raises(ValueError, match="payload"):
            NotificationPayload.decode(bad)
