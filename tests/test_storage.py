import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from release_monitor.models import (
    NotificationState,
    Origin,
    PublishedAtKind,
    Release,
    ReleaseKind,
    ReleaseStatus,
    Source,
)
from release_monitor.storage import open_database
from release_monitor.storage.db import transaction
from release_monitor.storage.repository import (
    DuplicateNotificationError,
    InvalidStateError,
    Repository,
)

from .helpers import T0, FakeClock, release

NEO = Source.NEOFORGE
MC = Source.MINECRAFT
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "releases.sqlite3"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_database(db_path)) as connection:
        yield connection


@pytest.fixture
def repo(conn: sqlite3.Connection, clock: FakeClock) -> Repository:
    return Repository(conn, clock=clock)


def queue(repo: Repository, version: str, *, origin: Origin = Origin.LIVE) -> int:
    repo.add_discovered(release(version), ReleaseStatus.QUEUED)
    return repo.enqueue(NEO, version, f"payload {version}", origin)


def test_a_fresh_database_is_not_initialized(repo: Repository) -> None:
    assert repo.is_initialized(NEO) is False
    assert repo.known_versions(NEO) == set()


def test_baseline_stores_every_release_even_those_filters_would_exclude(
    repo: Repository,
) -> None:
    """G1 regression: toggling NEOFORGE_BETAS/MINECRAFT_SNAPSHOTS later must not flood."""
    discovered = [
        release("26.2.0.57"),
        release("26.2.0.58-beta", kind=ReleaseKind.BETA),
        release("26.1.0.0-alpha.1+snapshot-1", kind=ReleaseKind.ALPHA),
    ]
    assert repo.baseline(NEO, discovered) == 3
    assert repo.is_initialized(NEO) is True
    assert repo.known_versions(NEO) == {
        "26.2.0.57",
        "26.2.0.58-beta",
        "26.1.0.0-alpha.1+snapshot-1",
    }
    stored = repo.get_release(NEO, "26.2.0.58-beta")
    assert stored is not None
    assert stored.status is ReleaseStatus.BASELINED
    assert stored.release.kind is ReleaseKind.BETA
    assert repo.claim_next_due(T0 + timedelta(days=1)) is None


def test_baseline_is_idempotent_and_keeps_existing_rows(repo: Repository) -> None:
    repo.baseline(NEO, [release("1.0")])
    repo.set_release_status(NEO, "1.0", ReleaseStatus.ANNOUNCED)
    assert repo.baseline(NEO, [release("1.0"), release("2.0")]) == 1
    existing = repo.get_release(NEO, "1.0")
    assert existing is not None
    assert existing.status is ReleaseStatus.ANNOUNCED


def test_baseline_rejects_releases_of_another_source(repo: Repository) -> None:
    with pytest.raises(ValueError, match="source"):
        repo.baseline(NEO, [release("26.3", source=MC)])
    assert repo.is_initialized(NEO) is False


def test_baseline_is_all_or_nothing(repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Repository._insert_release
    calls = {"n": 0}

    def flaky(self: Repository, *args: object, **kwargs: object) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Repository, "_insert_release", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        repo.baseline(NEO, [release("1.0"), release("2.0"), release("3.0")])
    assert repo.known_versions(NEO) == set()
    assert repo.is_initialized(NEO) is False


def test_add_discovered_ignores_duplicates(repo: Repository) -> None:
    assert repo.add_discovered(release("1.0"), ReleaseStatus.FILTERED) is True
    assert repo.add_discovered(release("1.0"), ReleaseStatus.QUEUED) is False
    stored = repo.get_release(NEO, "1.0")
    assert stored is not None
    assert stored.status is ReleaseStatus.FILTERED


def test_release_round_trips_all_fields(repo: Repository) -> None:
    published = datetime(2026, 9, 15, 11, 23, 2, tzinfo=UTC)
    original = Release(
        source=MC,
        version="26.3",
        kind=ReleaseKind.STABLE,
        published_at=published,
        published_at_kind=PublishedAtKind.OFFICIAL,
        mc_version="26.3",
        url="https://www.minecraft.net/en-us/article/minecraft-java-edition-26-3",
    )
    repo.add_discovered(original, ReleaseStatus.QUEUED)
    stored = repo.get_release(MC, "26.3")
    assert stored is not None
    assert stored.release == original
    assert stored.first_seen_at == T0


def test_set_release_status_and_deferred_listing(repo: Repository, clock: FakeClock) -> None:
    repo.add_discovered(release("1.0"), ReleaseStatus.DEFERRED)
    clock.advance(minutes=5)
    repo.add_discovered(release("2.0"), ReleaseStatus.DEFERRED)
    repo.add_discovered(release("3.0"), ReleaseStatus.FILTERED)
    deferred = repo.deferred_releases(NEO)
    assert [item.release.version for item in deferred] == ["1.0", "2.0"]
    assert deferred[0].first_seen_at == T0
    repo.set_release_status(NEO, "1.0", ReleaseStatus.QUEUED)
    assert [item.release.version for item in repo.deferred_releases(NEO)] == ["2.0"]
    with pytest.raises(KeyError):
        repo.set_release_status(NEO, "9.9", ReleaseStatus.QUEUED)


def test_happy_path_enqueue_claim_send(repo: Repository, clock: FakeClock) -> None:
    notification_id = queue(repo, "26.3.0.76")
    claimed = repo.claim_next_due(clock())
    assert claimed is not None
    assert claimed.id == notification_id
    assert claimed.state is NotificationState.SENDING
    assert claimed.attempts == 1
    assert claimed.attempt_id
    assert claimed.payload == "payload 26.3.0.76"
    assert repo.claim_next_due(clock()) is None  # nothing else is due

    clock.advance(seconds=2)
    repo.mark_sent(claimed.id, 4711, clock())
    done = repo.get_notification(claimed.id)
    assert done is not None
    assert done.state is NotificationState.SENT
    assert done.telegram_message_id == 4711
    assert done.sent_at == clock()
    stored = repo.get_release(NEO, "26.3.0.76")
    assert stored is not None
    assert stored.status is ReleaseStatus.ANNOUNCED


def test_duplicate_enqueue_is_rejected(repo: Repository) -> None:
    queue(repo, "1.0")
    with pytest.raises(DuplicateNotificationError):
        repo.enqueue(NEO, "1.0", "again", Origin.LIVE)


def test_enqueue_requires_a_known_release(repo: Repository) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue(NEO, "ghost", "x", Origin.LIVE)


def test_claim_orders_by_due_time_and_skips_future_rows(repo: Repository, clock: FakeClock) -> None:
    queue(repo, "1.0")
    clock.advance(seconds=1)
    queue(repo, "2.0")
    first = repo.claim_next_due(clock())
    second = repo.claim_next_due(clock())
    assert first is not None
    assert second is not None
    assert (first.version, second.version) == ("1.0", "2.0")

    repo.mark_retry(first.id, clock() + timedelta(minutes=5), "http_503")
    assert repo.claim_next_due(clock()) is None  # not due yet
    clock.advance(minutes=6)
    again = repo.claim_next_due(clock())
    assert again is not None
    assert again.id == first.id
    assert again.attempts == 2
    assert again.attempt_id != first.attempt_id
    assert again.last_error_class == "http_503"


def test_failed_permanent_is_terminal(repo: Repository, clock: FakeClock) -> None:
    queue(repo, "1.0")
    claimed = repo.claim_next_due(clock())
    assert claimed is not None
    repo.mark_failed_permanent(claimed.id, "http_400")
    failed = repo.get_notification(claimed.id)
    assert failed is not None
    assert failed.state is NotificationState.FAILED_PERMANENT
    assert failed.last_error_class == "http_400"
    clock.advance(days=1)
    assert repo.claim_next_due(clock()) is None


def test_invalid_state_transitions_are_rejected(repo: Repository, clock: FakeClock) -> None:
    notification_id = queue(repo, "1.0")
    with pytest.raises(InvalidStateError):
        repo.mark_sent(notification_id, 1, clock())  # still pending
    with pytest.raises(InvalidStateError):
        repo.mark_retry(notification_id, clock(), "x")
    with pytest.raises(InvalidStateError):
        repo.mark_sent(9999, 1, clock())


def test_state_survives_a_restart(db_path: Path, clock: FakeClock) -> None:
    with closing(open_database(db_path)) as first:
        repo = Repository(first, clock=clock)
        repo.baseline(NEO, [release("1.0")])
        queue(repo, "2.0")
        queue(repo, "3.0")
        claimed = repo.claim_next_due(clock())
        assert claimed is not None
        repo.mark_sent(claimed.id, 1, clock())
    with closing(open_database(db_path)) as second:
        repo = Repository(second, clock=clock)
        assert repo.is_initialized(NEO)
        assert repo.known_versions(NEO) == {"1.0", "2.0", "3.0"}
        nxt = repo.claim_next_due(clock())
        assert nxt is not None
        assert nxt.version == "3.0"  # the one that was never sent
        assert repo.claim_next_due(clock()) is None


def crash_while_sending(db_path: Path, clock: FakeClock) -> int:
    """Leave a notification in ``sending`` as if the process died mid-request."""
    connection = open_database(db_path)
    repo = Repository(connection, clock=clock)
    queue(repo, "1.0")
    claimed = repo.claim_next_due(clock())
    assert claimed is not None
    connection.close()
    return claimed.id


def test_crash_while_sending_is_retried_by_default_policy(db_path: Path, clock: FakeClock) -> None:
    notification_id = crash_while_sending(db_path, clock)
    clock.advance(minutes=30)
    with closing(open_database(db_path)) as conn:
        repo = Repository(conn, clock=clock)
        recovered = repo.recover_stale_sending(clock(), timedelta(minutes=10), "retry")
        assert [item.id for item in recovered] == [notification_id]
        again = repo.claim_next_due(clock())
        assert again is not None
        assert again.id == notification_id
        assert again.attempts == 2
        assert again.last_error_class == "ambiguous_retry"


def test_crash_while_sending_can_be_skipped_by_policy(db_path: Path, clock: FakeClock) -> None:
    notification_id = crash_while_sending(db_path, clock)
    clock.advance(minutes=30)
    with closing(open_database(db_path)) as conn:
        repo = Repository(conn, clock=clock)
        repo.recover_stale_sending(clock(), timedelta(minutes=10), "skip")
        skipped = repo.get_notification(notification_id)
        assert skipped is not None
        assert skipped.state is NotificationState.FAILED_PERMANENT
        assert skipped.last_error_class == "ambiguous_skipped"
        assert repo.claim_next_due(clock()) is None


def test_recent_sending_rows_are_not_recovered(repo: Repository, clock: FakeClock) -> None:
    queue(repo, "1.0")
    assert repo.claim_next_due(clock()) is not None
    clock.advance(minutes=1)
    assert repo.recover_stale_sending(clock(), timedelta(minutes=10), "retry") == []


def test_unknown_policy_is_rejected(repo: Repository, clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="policy"):
        repo.recover_stale_sending(clock(), timedelta(minutes=1), "maybe")


def test_transaction_rollback_leaves_no_partial_state(
    conn: sqlite3.Connection, repo: Repository
) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO meta VALUES ('k', 'v')")
        raise RuntimeError("abort")
    assert repo.get_meta("k") is None


def test_meta_round_trip_and_typed_helpers(repo: Repository, clock: FakeClock) -> None:
    assert repo.get_meta("missing") is None
    repo.set_meta("k", "one")
    repo.set_meta("k", "two")
    assert repo.get_meta("k") == "two"
    assert repo.last_success(NEO) is None
    repo.set_last_success(NEO, clock())
    repo.set_last_poll(clock())
    assert repo.last_success(NEO) == T0
    assert repo.last_poll() == T0
    assert repo.get_meta("last_success:neoforge") is not None
    assert repo.get_meta("last_poll_at") is not None


def test_timestamps_are_stored_as_utc_iso_strings(
    conn: sqlite3.Connection, repo: Repository, clock: FakeClock
) -> None:
    queue(repo, "1.0")
    row = conn.execute(
        "SELECT created_at, next_attempt_at, updated_at FROM notifications"
    ).fetchone()
    assert all(ISO.match(value) for value in row)
    first_seen = conn.execute("SELECT first_seen_at, discovered_at FROM releases").fetchone()
    assert all(ISO.match(value) for value in first_seen)


def test_non_utc_aware_datetimes_are_normalised(conn: sqlite3.Connection, repo: Repository) -> None:
    madrid = timezone(timedelta(hours=2))
    stamp = datetime(2026, 9, 15, 13, 23, 2, tzinfo=madrid)
    repo.add_discovered(release("1.0", published_at=stamp), ReleaseStatus.QUEUED)
    raw = conn.execute("SELECT published_at FROM releases").fetchone()[0]
    assert raw == "2026-09-15T11:23:02.000000Z"


def test_naive_datetimes_are_rejected(repo: Repository) -> None:
    with pytest.raises(ValueError, match="timezone"):
        repo.add_discovered(
            release("1.0", published_at=datetime(2026, 1, 1)),
            ReleaseStatus.QUEUED,
        )
    with pytest.raises(ValueError, match="timezone"):
        repo.claim_next_due(datetime(2026, 1, 1))
