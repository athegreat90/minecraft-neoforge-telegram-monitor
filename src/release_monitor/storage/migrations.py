"""Schema creation and migration, driven by ``PRAGMA user_version``.

* version 0 + no tables: a fresh database, created directly at the latest version.
* version 0 + legacy ``sent``/``initialized`` tables: the pre-package ``monitor.py`` schema ("v1").
  It is copied to ``<db>.bak-v1`` first, then migrated in one transaction. The legacy ``sent``
  table is left in place so rolling back to the old image still finds its history.
* version > SCHEMA_VERSION: refused, never modified.
"""

import logging
import re
import sqlite3
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from release_monitor.models import (
    NotificationState,
    Origin,
    PublishedAtKind,
    ReleaseKind,
    ReleaseStatus,
    Source,
)
from release_monitor.storage.db import StorageError, backup, transaction
from release_monitor.timeutil import from_iso, to_iso, utc_now

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 2
LEGACY_BACKUP_SUFFIX = ".bak-v1"
_LEGACY_TABLES = frozenset({"sent", "initialized"})
_MC_SNAPSHOT = re.compile(r"(-snapshot-\d+$|^\d{2}w\d{2}[a-z]$)")


class MigrationError(StorageError):
    """The database cannot be migrated safely."""


class SchemaVersionError(MigrationError):
    """The database was written by a newer version of this application."""


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


_V2_STATEMENTS = (
    f"""CREATE TABLE releases (
        source TEXT NOT NULL CHECK (source IN ({_values(Source)})),
        version TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ({_values(ReleaseKind)})),
        published_at TEXT,
        published_at_kind TEXT NOT NULL DEFAULT 'detected'
            CHECK (published_at_kind IN ({_values(PublishedAtKind)})),
        mc_version TEXT,
        url TEXT,
        status TEXT NOT NULL CHECK (status IN ({_values(ReleaseStatus)})),
        first_seen_at TEXT NOT NULL,  -- first poll that saw it; drives the changelog grace window
        discovered_at TEXT NOT NULL,  -- when this row was inserted
        PRIMARY KEY (source, version)
    )""",
    f"""CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({_values(NotificationState)})),
        payload TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        attempt_id TEXT,
        next_attempt_at TEXT NOT NULL,
        last_error_class TEXT,
        telegram_message_id INTEGER,
        origin TEXT NOT NULL CHECK (origin IN ({_values(Origin)})),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,  -- last state change; used to spot stale 'sending' rows
        sent_at TEXT,
        UNIQUE (source, version),
        FOREIGN KEY (source, version) REFERENCES releases (source, version)
    )""",
    "CREATE INDEX notifications_due ON notifications (state, next_attempt_at)",
    "CREATE TABLE IF NOT EXISTS initialized (source TEXT PRIMARY KEY, started_at TEXT NOT NULL)",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`, or raise without changing anything."""
    version = _user_version(conn)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema version {version} is newer than this application supports "
            f"({SCHEMA_VERSION}); upgrade the application instead of reusing this database"
        )
    if version == SCHEMA_VERSION:
        return
    tables = _table_names(conn)
    if not tables:
        with transaction(conn):
            _create_v2(conn)
        LOG.info("created database schema v%d", SCHEMA_VERSION)
    elif tables <= _LEGACY_TABLES:
        _backup_legacy(conn, db_path)
        with transaction(conn):
            _migrate_legacy(conn)
        LOG.info("migrated legacy database to schema v%d", SCHEMA_VERSION)
    else:
        raise MigrationError(
            f"unrecognised database (tables: {', '.join(sorted(tables))}); refusing to modify it"
        )


def _create_v2(conn: sqlite3.Connection) -> None:
    for statement in _V2_STATEMENTS:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _backup_legacy(conn: sqlite3.Connection, db_path: Path) -> None:
    dest = db_path.with_name(db_path.name + LEGACY_BACKUP_SUFFIX)
    if dest.exists():
        LOG.info("legacy backup %s already exists; keeping it", dest.name)
        return
    partial = dest.with_name(dest.name + ".tmp")
    partial.unlink(missing_ok=True)
    backup(conn, partial)
    partial.replace(dest)
    LOG.info("wrote legacy backup %s", dest.name)


def _legacy_kind(source: str, version: str) -> str:
    """Best-effort kind for rows written before kinds were recorded."""
    if source == Source.MINECRAFT:
        if _MC_SNAPSHOT.search(version):
            return ReleaseKind.SNAPSHOT.value
        if "-pre-" in version:
            return ReleaseKind.PRERELEASE.value
        if "-rc-" in version:
            return ReleaseKind.RELEASE_CANDIDATE.value
        return ReleaseKind.STABLE.value
    if "-alpha" in version:
        return ReleaseKind.ALPHA.value
    if "-beta" in version:
        return ReleaseKind.BETA.value
    return ReleaseKind.STABLE.value


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    now = to_iso(utc_now())
    legacy_tables = _table_names(conn)
    sent: Iterable[sqlite3.Row] = (
        conn.execute("SELECT source, version, sent_at FROM sent").fetchall()
        if "sent" in legacy_tables
        else []
    )
    started = {
        row["source"]: _normalise(row["started_at"], now)
        for row in conn.execute("SELECT source, started_at FROM initialized").fetchall()
    }
    _create_v2(conn)
    known = {member.value for member in Source}
    for row in sent:
        source, version = row["source"], row["version"]
        if source not in known:
            LOG.warning("skipping legacy row with unknown source %r", source)
            continue
        seen_at = _normalise(row["sent_at"], now)
        # The old code seeded existing versions with the exact timestamp it stored in
        # `initialized`; anything announced later carries its own, later timestamp.
        status = (
            ReleaseStatus.BASELINED if started.get(source) == seen_at else ReleaseStatus.ANNOUNCED
        )
        conn.execute(
            "INSERT INTO releases (source, version, kind, published_at, published_at_kind, "
            "mc_version, url, status, first_seen_at, discovered_at) "
            "VALUES (?, ?, ?, NULL, 'detected', NULL, NULL, ?, ?, ?)",
            (source, version, _legacy_kind(source, version), status.value, seen_at, seen_at),
        )
        conn.execute(
            "INSERT INTO notifications (source, version, state, payload, attempts, "
            "next_attempt_at, origin, created_at, updated_at, sent_at) "
            "VALUES (?, ?, 'sent', '', 0, ?, 'legacy', ?, ?, ?)",
            (source, version, seen_at, seen_at, seen_at, seen_at),
        )


def _normalise(stamp: str, fallback: str) -> str:
    try:
        return to_iso(from_iso(stamp))
    except ValueError:
        return fallback
