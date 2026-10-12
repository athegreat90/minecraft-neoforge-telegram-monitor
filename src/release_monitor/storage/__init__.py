"""SQLite persistence: connection, migrations, repository and the instance lock."""

import sqlite3
from pathlib import Path

from release_monitor.storage.db import (
    AlreadyRunningError,
    DatabaseCorruptError,
    InstanceLock,
    StorageError,
    backup,
    connect,
    integrity_check,
    transaction,
)
from release_monitor.storage.migrations import SCHEMA_VERSION, migrate
from release_monitor.storage.repository import Repository


def open_database(path: Path) -> sqlite3.Connection:
    """Open ``path``, verify its integrity and migrate it to the current schema.

    The connection is closed again if either step fails.
    """
    conn = connect(path)
    try:
        integrity_check(conn)
        migrate(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


__all__ = [
    "SCHEMA_VERSION",
    "AlreadyRunningError",
    "DatabaseCorruptError",
    "InstanceLock",
    "Repository",
    "StorageError",
    "backup",
    "connect",
    "integrity_check",
    "migrate",
    "open_database",
    "transaction",
]
