"""Typed persistence API: release catalogue, notification outbox and metadata."""

import logging
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from release_monitor.models import (
    Notification,
    NotificationState,
    Origin,
    PublishedAtKind,
    Release,
    ReleaseKind,
    ReleaseStatus,
    Source,
    StoredRelease,
)
from release_monitor.storage.db import StorageError, transaction
from release_monitor.timeutil import from_iso, to_iso, utc_now

LOG = logging.getLogger(__name__)

AMBIGUOUS_POLICIES = frozenset({"retry", "skip"})


class DuplicateNotificationError(StorageError):
    """A notification for this release already exists."""


class InvalidStateError(StorageError):
    """The notification is not in a state that allows the requested transition."""


class Repository:
    """All reads and writes go through here so state changes stay transactional."""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    # -- release catalogue ------------------------------------------------------------

    def is_initialized(self, source: Source) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM initialized WHERE source = ?", (source.value,)
        ).fetchone()
        return row is not None

    def baseline(self, source: Source, releases: Sequence[Release]) -> int:
        """Record every given release as already known and mark the source initialised.

        Filters are deliberately not applied here: later enabling betas or snapshots must
        not announce history. Returns how many rows were new.
        """
        if any(item.source != source for item in releases):
            raise ValueError(f"all releases must have source {source.value!r}")
        now = to_iso(self._clock())
        with transaction(self._conn):
            inserted = sum(
                self._insert_release(item, ReleaseStatus.BASELINED, now) for item in releases
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO initialized (source, started_at) VALUES (?, ?)",
                (source.value, now),
            )
        return inserted

    def known_versions(self, source: Source) -> set[str]:
        rows = self._conn.execute(
            "SELECT version FROM releases WHERE source = ?", (source.value,)
        ).fetchall()
        return {row["version"] for row in rows}

    def add_discovered(self, release: Release, status: ReleaseStatus) -> bool:
        """Insert a newly seen release. Returns ``False`` if it was already known."""
        now = to_iso(self._clock())
        with transaction(self._conn):
            return self._insert_release(release, status, now)

    def _insert_release(self, release: Release, status: ReleaseStatus, now: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO releases (source, version, kind, published_at, "
            "published_at_kind, mc_version, url, status, first_seen_at, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                release.source.value,
                release.version,
                release.kind.value,
                to_iso(release.published_at) if release.published_at else None,
                release.published_at_kind.value,
                release.mc_version,
                release.url,
                status.value,
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def set_release_status(self, source: Source, version: str, status: ReleaseStatus) -> None:
        cursor = self._conn.execute(
            "UPDATE releases SET status = ? WHERE source = ? AND version = ?",
            (status.value, source.value, version),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"unknown release {source.value} {version}")

    def get_release(self, source: Source, version: str) -> StoredRelease | None:
        row = self._conn.execute(
            "SELECT * FROM releases WHERE source = ? AND version = ?", (source.value, version)
        ).fetchone()
        return _stored_release(row) if row else None

    def deferred_releases(self, source: Source) -> list[StoredRelease]:
        rows = self._conn.execute(
            "SELECT * FROM releases WHERE source = ? AND status = ? "
            "ORDER BY first_seen_at, version",
            (source.value, ReleaseStatus.DEFERRED.value),
        ).fetchall()
        return [_stored_release(row) for row in rows]

    # -- notification outbox ------------------------------------------------------------

    def enqueue(
        self,
        source: Source,
        version: str,
        payload: str,
        origin: Origin,
        *,
        next_attempt_at: datetime | None = None,
    ) -> int:
        """Queue the announcement of an existing release; returns the notification id."""
        now = self._clock()
        due = to_iso(next_attempt_at or now)
        stamp = to_iso(now)
        with transaction(self._conn):
            exists = self._conn.execute(
                "SELECT 1 FROM notifications WHERE source = ? AND version = ?",
                (source.value, version),
            ).fetchone()
            if exists:
                raise DuplicateNotificationError(
                    f"notification for {source.value} {version} already exists"
                )
            cursor = self._conn.execute(
                "INSERT INTO notifications (source, version, state, payload, attempts, "
                "next_attempt_at, origin, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, 0, ?, ?, ?, ?)",
                (source.value, version, payload, due, origin.value, stamp, stamp),
            )
            self._conn.execute(
                "UPDATE releases SET status = ? WHERE source = ? AND version = ?",
                (ReleaseStatus.QUEUED.value, source.value, version),
            )
        if cursor.lastrowid is None:  # pragma: no cover - sqlite always sets it after INSERT
            raise StorageError("insert did not return a row id")
        return cursor.lastrowid

    def claim_next_due(self, now: datetime) -> Notification | None:
        """Atomically move the oldest due ``pending`` row to ``sending`` and return it."""
        stamp = to_iso(now)
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT id FROM notifications WHERE state = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, id LIMIT 1",
                (stamp,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE notifications SET state = 'sending', attempts = attempts + 1, "
                "attempt_id = ?, updated_at = ? WHERE id = ?",
                (uuid.uuid4().hex, stamp, row["id"]),
            )
        return self.get_notification(row["id"])

    def mark_sent(self, notification_id: int, message_id: int | None, now: datetime) -> None:
        stamp = to_iso(now)
        with transaction(self._conn):
            self._transition(
                notification_id,
                {NotificationState.SENDING},
                "state = 'sent', telegram_message_id = ?, sent_at = ?, updated_at = ?",
                (message_id, stamp, stamp),
            )
            self._conn.execute(
                "UPDATE releases SET status = ? WHERE (source, version) = "
                "(SELECT source, version FROM notifications WHERE id = ?)",
                (ReleaseStatus.ANNOUNCED.value, notification_id),
            )

    def mark_retry(self, notification_id: int, next_attempt_at: datetime, error_class: str) -> None:
        with transaction(self._conn):
            self._transition(
                notification_id,
                {NotificationState.SENDING},
                "state = 'pending', next_attempt_at = ?, last_error_class = ?, updated_at = ?",
                (to_iso(next_attempt_at), error_class, to_iso(self._clock())),
            )

    def mark_failed_permanent(self, notification_id: int, error_class: str) -> None:
        with transaction(self._conn):
            self._transition(
                notification_id,
                {NotificationState.SENDING, NotificationState.PENDING},
                "state = 'failed_permanent', last_error_class = ?, updated_at = ?",
                (error_class, to_iso(self._clock())),
            )

    def recover_stale_sending(
        self, now: datetime, older_than: timedelta, policy: str
    ) -> list[Notification]:
        """Resolve ``sending`` rows left behind by a crash; their delivery outcome is unknown.

        ``retry`` re-queues them (a duplicate post is possible); ``skip`` gives up on them
        (a missed post is possible).
        """
        if policy not in AMBIGUOUS_POLICIES:
            raise ValueError(f"unknown ambiguous-delivery policy {policy!r}")
        stamp = to_iso(now)
        cutoff = to_iso(now - older_than)
        with transaction(self._conn):
            stale = self._conn.execute(
                "SELECT id, source, version, attempts FROM notifications "
                "WHERE state = 'sending' AND updated_at <= ? ORDER BY id",
                (cutoff,),
            ).fetchall()
            for row in stale:
                if policy == "retry":
                    LOG.warning(
                        "ambiguous retry of %s %s (attempt %d): the previous send may have "
                        "reached Telegram, so a duplicate post is possible",
                        row["source"],
                        row["version"],
                        row["attempts"],
                    )
                    sql = (
                        "UPDATE notifications SET state = 'pending', next_attempt_at = ?, "
                        "last_error_class = 'ambiguous_retry', updated_at = ? WHERE id = ?"
                    )
                    params: tuple[object, ...] = (stamp, stamp, row["id"])
                else:
                    LOG.warning(
                        "skipping %s %s: previous send outcome unknown and policy is 'skip'",
                        row["source"],
                        row["version"],
                    )
                    sql = (
                        "UPDATE notifications SET state = 'failed_permanent', "
                        "last_error_class = 'ambiguous_skipped', updated_at = ? WHERE id = ?"
                    )
                    params = (stamp, row["id"])
                self._conn.execute(sql, params)
        recovered = (self.get_notification(row["id"]) for row in stale)
        return [item for item in recovered if item is not None]

    def get_notification(self, notification_id: int) -> Notification | None:
        row = self._conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        return _notification(row) if row else None

    def _transition(
        self,
        notification_id: int,
        allowed: set[NotificationState],
        assignments: str,
        params: tuple[object, ...],
    ) -> None:
        marks = ", ".join("?" for _ in allowed)
        cursor = self._conn.execute(
            f"UPDATE notifications SET {assignments} WHERE id = ? AND state IN ({marks})",  # noqa: S608
            (*params, notification_id, *(state.value for state in allowed)),
        )
        if cursor.rowcount == 0:
            raise InvalidStateError(
                f"notification {notification_id} is missing or not in state "
                f"{' / '.join(sorted(state.value for state in allowed))}"
            )

    # -- metadata -----------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def set_last_success(self, source: Source, when: datetime) -> None:
        self.set_meta(f"last_success:{source.value}", to_iso(when))

    def last_success(self, source: Source) -> datetime | None:
        return self._meta_time(f"last_success:{source.value}")

    def set_last_poll(self, when: datetime) -> None:
        self.set_meta("last_poll_at", to_iso(when))

    def last_poll(self) -> datetime | None:
        return self._meta_time("last_poll_at")

    def _meta_time(self, key: str) -> datetime | None:
        value = self.get_meta(key)
        return from_iso(value) if value else None


def _stored_release(row: sqlite3.Row) -> StoredRelease:
    return StoredRelease(
        release=Release(
            source=Source(row["source"]),
            version=row["version"],
            kind=ReleaseKind(row["kind"]),
            published_at=from_iso(row["published_at"]) if row["published_at"] else None,
            published_at_kind=PublishedAtKind(row["published_at_kind"]),
            mc_version=row["mc_version"],
            url=row["url"],
        ),
        status=ReleaseStatus(row["status"]),
        first_seen_at=from_iso(row["first_seen_at"]),
        discovered_at=from_iso(row["discovered_at"]),
    )


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        source=Source(row["source"]),
        version=row["version"],
        state=NotificationState(row["state"]),
        payload=row["payload"],
        attempts=row["attempts"],
        attempt_id=row["attempt_id"],
        next_attempt_at=from_iso(row["next_attempt_at"]),
        last_error_class=row["last_error_class"],
        telegram_message_id=row["telegram_message_id"],
        origin=Origin(row["origin"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        sent_at=from_iso(row["sent_at"]) if row["sent_at"] else None,
    )
