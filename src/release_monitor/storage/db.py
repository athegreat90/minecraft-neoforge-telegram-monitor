"""SQLite connection handling, backups and the single-instance lock."""

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import IO, Self

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

BUSY_TIMEOUT_MS = 5000


class StorageError(Exception):
    """Base class for storage problems."""


class DatabaseCorruptError(StorageError):
    """The database file is damaged. It is never deleted automatically."""


class AlreadyRunningError(StorageError):
    """Another process already holds the instance lock for this database."""


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in autocommit mode; callers use :func:`transaction` for writes.

    The default rollback journal is used on purpose (not WAL): WAL is unsafe on the
    network file systems some NAS setups bind-mount, and write volume here is tiny.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.DatabaseError as exc:  # e.g. "file is not a database"
        conn.close()
        raise DatabaseCorruptError(_corrupt_message(str(exc))) from exc
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, or ``ROLLBACK`` if the block raises."""
    if conn.in_transaction:
        raise RuntimeError("nested transactions are not supported")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def integrity_check(conn: sqlite3.Connection) -> None:
    """Raise :class:`DatabaseCorruptError` unless ``PRAGMA integrity_check`` says ``ok``."""
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptError(_corrupt_message(str(exc))) from exc
    if rows != ["ok"]:
        raise DatabaseCorruptError(_corrupt_message("; ".join(rows[:5])))


def _corrupt_message(detail: str) -> str:
    return (
        f"SQLite database is damaged ({detail}). The file was left untouched: "
        "restore a backup, or move it aside and re-baseline to start fresh."
    )


def backup(conn: sqlite3.Connection, dest: Path) -> None:
    """Write a consistent copy with ``VACUUM INTO``. Never overwrites ``dest``."""
    if dest.exists():
        raise FileExistsError(f"backup target already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(dest),))


class InstanceLock:
    """Non-blocking, cross-platform lock so two processes never share one database."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.parent / ".lock"
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            raise AlreadyRunningError(
                f"another instance is already using {self._path.parent}; "
                "run a single monitor per database"
            ) from None
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform == "win32":

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
