"""The health check: local state only, never the network, never secrets."""

from datetime import timedelta
from pathlib import Path

import pytest

from release_monitor.health import (
    check_health,
    heartbeat_path,
    write_heartbeat,
)
from release_monitor.models import Source
from release_monitor.storage import Repository, open_database
from tests.helpers import T0

POLL = 300


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "releases.sqlite3"


def record_success(db_path: Path, *sources: Source, at=T0) -> None:  # type: ignore[no-untyped-def]
    conn = open_database(db_path)
    try:
        repo = Repository(conn)
        for source in sources:
            repo.set_last_success(source, at)
    finally:
        conn.close()


def test_write_heartbeat_is_atomic_and_readable(db_path: Path) -> None:
    write_heartbeat(db_path, T0)
    write_heartbeat(db_path, T0 + timedelta(seconds=5))

    assert heartbeat_path(db_path).read_text(encoding="utf-8").startswith("2026-10-01T10:00:05")
    assert not list(db_path.parent.glob("*.tmp"))


def test_unhealthy_without_a_heartbeat(db_path: Path) -> None:
    status = check_health(db_path, POLL, T0)

    assert not status.ok
    assert "no heartbeat" in status.reason


def test_unhealthy_with_an_unreadable_heartbeat(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    heartbeat_path(db_path).write_text("not a timestamp", encoding="utf-8")

    status = check_health(db_path, POLL, T0)

    assert not status.ok
    assert "unreadable" in status.reason


def test_unhealthy_when_the_heartbeat_is_stale(db_path: Path) -> None:
    record_success(db_path, Source.MINECRAFT, at=T0)
    write_heartbeat(db_path, T0)

    status = check_health(db_path, POLL, T0 + timedelta(seconds=3 * POLL + 61))

    assert not status.ok
    assert "stale" in status.reason


def test_heartbeat_within_three_cycles_is_fresh(db_path: Path) -> None:
    record_success(db_path, Source.MINECRAFT, at=T0)
    write_heartbeat(db_path, T0)

    assert check_health(db_path, POLL, T0 + timedelta(seconds=3 * POLL + 59)).ok


def test_unhealthy_when_the_database_is_missing(db_path: Path) -> None:
    write_heartbeat(db_path, T0)

    status = check_health(db_path, POLL, T0)

    assert not status.ok
    assert "does not exist" in status.reason


def test_unhealthy_when_the_database_is_not_a_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"this is definitely not sqlite" * 50)
    write_heartbeat(db_path, T0)

    status = check_health(db_path, POLL, T0)

    assert not status.ok
    assert "cannot be" in status.reason


def test_unhealthy_when_no_source_ever_succeeded(db_path: Path) -> None:
    record_success(db_path)
    write_heartbeat(db_path, T0)

    status = check_health(db_path, POLL, T0)

    assert not status.ok
    assert "no source was polled successfully" in status.reason


def test_unhealthy_when_the_last_success_is_too_old(db_path: Path) -> None:
    record_success(db_path, Source.MINECRAFT, Source.NEOFORGE, at=T0)
    later = T0 + timedelta(seconds=6 * POLL + 1)
    write_heartbeat(db_path, later)

    status = check_health(db_path, POLL, later)

    assert not status.ok


def test_one_recent_source_is_enough(db_path: Path) -> None:
    record_success(db_path, Source.MINECRAFT, at=T0 - timedelta(days=2))
    record_success(db_path, Source.NEOFORGE, at=T0 - timedelta(seconds=30))
    write_heartbeat(db_path, T0)

    status = check_health(db_path, POLL, T0)

    assert status.ok
    assert "neoforge 30s ago" in status.reason
    assert "minecraft" not in status.reason


def test_the_check_does_not_modify_the_database(db_path: Path) -> None:
    record_success(db_path, Source.MINECRAFT, at=T0)
    write_heartbeat(db_path, T0)
    before = db_path.read_bytes()

    assert check_health(db_path, POLL, T0).ok

    assert db_path.read_bytes() == before
