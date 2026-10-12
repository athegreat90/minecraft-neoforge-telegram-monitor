import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from release_monitor.storage.db import (
    AlreadyRunningError,
    DatabaseCorruptError,
    InstanceLock,
    backup,
    connect,
    integrity_check,
    transaction,
)


def test_connect_creates_parent_directories_and_sets_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "db.sqlite3"
    with closing(connect(path)) as conn:
        assert path.parent.is_dir()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"


def test_transaction_commits(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "db")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "db")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(RuntimeError, match="boom"), transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_nested_transactions_are_rejected(tmp_path: Path) -> None:
    with (
        closing(connect(tmp_path / "db")) as conn,
        transaction(conn),
        pytest.raises(RuntimeError, match="nested"),
        transaction(conn),
    ):
        pass  # pragma: no cover


def test_backup_produces_a_valid_copy(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "db")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        dest = tmp_path / "out" / "copy.sqlite3"
        backup(conn, dest)
    with closing(sqlite3.connect(dest)) as copy:
        assert copy.execute("SELECT x FROM t").fetchone()[0] == 42


def test_backup_never_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "copy.sqlite3"
    dest.write_bytes(b"precious")
    with closing(connect(tmp_path / "db")) as conn, pytest.raises(FileExistsError):
        backup(conn, dest)
    assert dest.read_bytes() == b"precious"


def test_integrity_check_passes_on_a_healthy_database(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "db")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        integrity_check(conn)


def test_connect_reports_a_non_database_file_and_never_deletes_it(tmp_path: Path) -> None:
    path = tmp_path / "db"
    path.write_bytes(b"this is definitely not a sqlite database" * 50)
    before = path.read_bytes()
    with pytest.raises(DatabaseCorruptError, match="restore"):
        connect(path)
    assert path.read_bytes() == before


def test_integrity_check_reports_page_damage_and_never_deletes(tmp_path: Path) -> None:
    path = tmp_path / "db"
    with closing(connect(path)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER PRIMARY KEY, y TEXT)")
        conn.execute("CREATE INDEX t_y ON t (y)")
        with transaction(conn):
            conn.executemany("INSERT INTO t (y) VALUES (?)", [(f"row-{i}",) for i in range(500)])
    data = bytearray(path.read_bytes())
    assert len(data) > 3 * 4096
    data[4096 : 2 * 4096] = b"\xff" * 4096  # destroy page 2
    path.write_bytes(bytes(data))
    before = path.read_bytes()
    with closing(connect(path)) as conn, pytest.raises(DatabaseCorruptError, match="restore"):
        integrity_check(conn)
    assert path.read_bytes() == before


def test_lock_file_lives_next_to_the_database(tmp_path: Path) -> None:
    with InstanceLock(tmp_path / "data" / "releases.sqlite3"):
        assert (tmp_path / "data" / ".lock").exists()


def test_second_instance_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "releases.sqlite3"
    first = InstanceLock(db_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match="already"):
            InstanceLock(db_path).acquire()
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    db_path = tmp_path / "releases.sqlite3"
    with InstanceLock(db_path):
        pass
    with InstanceLock(db_path):
        pass


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "db")
    lock.acquire()
    lock.release()
    lock.release()


def test_context_manager_releases_on_error(tmp_path: Path) -> None:
    db_path = tmp_path / "db"
    with pytest.raises(RuntimeError), InstanceLock(db_path):
        raise RuntimeError("fail")
    with InstanceLock(db_path):
        pass
