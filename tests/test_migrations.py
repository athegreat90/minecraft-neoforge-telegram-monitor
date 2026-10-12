import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from release_monitor.models import ReleaseStatus, Source
from release_monitor.storage import open_database
from release_monitor.storage.db import DatabaseCorruptError, connect
from release_monitor.storage.migrations import SCHEMA_VERSION, SchemaVersionError, migrate
from release_monitor.storage.repository import Repository

from .helpers import T0, build_legacy_db

BAK = ".bak-v1"


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def make_legacy(path: Path) -> None:
    build_legacy_db(
        path,
        seeded={"minecraft": ["1.21.4", "1.21.5"], "neoforge": ["26.2.0.57", "26.2.0.58-beta"]},
        sent_later={"minecraft": [("26.3", T0 + timedelta(days=3))]},
    )


def test_fresh_database_gets_schema_v2_without_backup(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    with closing(open_database(path)) as conn:
        assert user_version(conn) == SCHEMA_VERSION == 2
        assert {"releases", "notifications", "initialized", "meta"} <= tables(conn)
        assert "sent" not in tables(conn)
    assert not path.with_name(path.name + BAK).exists()


def test_legacy_v1_database_is_migrated_and_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)) as conn:
        assert user_version(conn) == 2
        repo = Repository(conn)
        assert repo.known_versions(Source.MINECRAFT) == {"1.21.4", "1.21.5", "26.3"}
        assert repo.known_versions(Source.NEOFORGE) == {"26.2.0.57", "26.2.0.58-beta"}
        assert repo.is_initialized(Source.MINECRAFT)
        assert repo.is_initialized(Source.NEOFORGE)
    backup_path = path.with_name(path.name + BAK)
    assert backup_path.exists()
    with closing(sqlite3.connect(backup_path)) as old:
        assert user_version(old) == 0
        assert old.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 5


def test_seeds_are_baselined_and_later_sends_are_announced(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)) as conn:
        repo = Repository(conn)
        seed = repo.get_release(Source.MINECRAFT, "1.21.4")
        later = repo.get_release(Source.MINECRAFT, "26.3")
        assert seed is not None
        assert later is not None
        assert seed.status is ReleaseStatus.BASELINED
        assert later.status is ReleaseStatus.ANNOUNCED
        assert later.release.published_at is None


def test_every_legacy_row_has_a_sent_legacy_notification(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)) as conn:
        rows = conn.execute("SELECT state, origin FROM notifications").fetchall()
        assert len(rows) == 5
        assert {(row["state"], row["origin"]) for row in rows} == {("sent", "legacy")}


def test_nothing_is_re_announceable_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)) as conn:
        repo = Repository(conn)
        assert repo.claim_next_due(T0 + timedelta(days=365)) is None
        assert repo.deferred_releases(Source.MINECRAFT) == []


def test_legacy_kinds_are_classified_best_effort(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    build_legacy_db(
        path,
        seeded={
            "minecraft": ["1.21.4", "26.4-snapshot-3", "26.3-pre-1", "26.3-rc-2", "25w46a"],
            "neoforge": ["26.2.0.57", "26.2.0.58-beta", "26.1.0.0-alpha.1+snapshot-1"],
        },
    )
    with closing(open_database(path)) as conn:
        kinds = {
            (row["source"], row["version"]): row["kind"]
            for row in conn.execute("SELECT source, version, kind FROM releases")
        }
    assert kinds[("minecraft", "1.21.4")] == "stable"
    assert kinds[("minecraft", "26.4-snapshot-3")] == "snapshot"
    assert kinds[("minecraft", "26.3-pre-1")] == "prerelease"
    assert kinds[("minecraft", "26.3-rc-2")] == "release_candidate"
    assert kinds[("minecraft", "25w46a")] == "snapshot"
    assert kinds[("neoforge", "26.2.0.57")] == "stable"
    assert kinds[("neoforge", "26.2.0.58-beta")] == "beta"
    assert kinds[("neoforge", "26.1.0.0-alpha.1+snapshot-1")] == "alpha"


def test_legacy_sent_table_is_left_untouched_for_rollback(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)) as conn:
        assert "sent" in tables(conn)
        assert conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 5


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    backup_path = path.with_name(path.name + BAK)
    backup_path.write_bytes(b"earlier backup")
    with closing(open_database(path)) as conn:
        assert user_version(conn) == 2
    assert backup_path.read_bytes() == b"earlier backup"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)
    with closing(open_database(path)):
        pass
    with closing(open_database(path)) as conn:
        assert user_version(conn) == 2
        assert conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 5


def test_failed_migration_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "releases.sqlite3"
    make_legacy(path)

    def explode(source: str, version: str) -> str:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("release_monitor.storage.migrations._legacy_kind", explode)
    with closing(connect(path)) as conn:
        with pytest.raises(RuntimeError, match="simulated"):
            migrate(conn, path)
        assert user_version(conn) == 0
        assert "releases" not in tables(conn)
        assert "notifications" not in tables(conn)
        assert conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 5


def test_newer_schema_is_refused_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE TABLE marker (x INTEGER)")
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
    with pytest.raises(SchemaVersionError, match="99"):
        open_database(path).close()
    with closing(sqlite3.connect(path)) as raw:
        assert user_version(raw) == 99
        assert tables(raw) == {"marker"}


def test_corrupt_file_is_reported_and_never_deleted(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    before = path.read_bytes()
    with pytest.raises(DatabaseCorruptError):
        open_database(path).close()
    assert path.read_bytes() == before


def test_damaged_header_of_a_real_database_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "releases.sqlite3"
    with closing(open_database(path)):
        pass
    data = bytearray(path.read_bytes())
    data[:100] = bytes(100)
    path.write_bytes(bytes(data))
    with pytest.raises(DatabaseCorruptError):
        open_database(path).close()
    assert path.exists()
