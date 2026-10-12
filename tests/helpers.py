"""Test helpers shared by the storage tests."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source

T0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)

LEGACY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sent "
    "(source TEXT NOT NULL, version TEXT NOT NULL, sent_at TEXT NOT NULL, "
    "PRIMARY KEY (source, version))",
    "CREATE TABLE IF NOT EXISTS initialized (source TEXT PRIMARY KEY, started_at TEXT NOT NULL)",
)


class FakeClock:
    """Deterministic, advanceable clock."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def release(
    version: str,
    *,
    source: Source = Source.NEOFORGE,
    kind: ReleaseKind = ReleaseKind.STABLE,
    published_at: datetime | None = None,
) -> Release:
    return Release(
        source=source,
        version=version,
        kind=kind,
        published_at=published_at,
        published_at_kind=PublishedAtKind.OFFICIAL if published_at else PublishedAtKind.DETECTED,
    )


def build_legacy_db(
    path: Path,
    *,
    seeded: dict[str, list[str]],
    sent_later: dict[str, list[tuple[str, datetime]]] | None = None,
    started_at: datetime = T0,
) -> None:
    """Create a database exactly as the old ``monitor.py`` left it.

    The old code seeded every existing version with the same ``now`` it wrote to
    ``initialized`` and wrote later announcements with their own timestamp.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in LEGACY_SCHEMA:
            conn.execute(statement)
        stamp = started_at.isoformat()
        for source, versions in seeded.items():
            conn.executemany(
                "INSERT INTO sent VALUES (?, ?, ?)", [(source, v, stamp) for v in versions]
            )
            conn.execute("INSERT INTO initialized VALUES (?, ?)", (source, stamp))
        for source, items in (sent_later or {}).items():
            conn.executemany(
                "INSERT INTO sent VALUES (?, ?, ?)",
                [(source, v, at.isoformat()) for v, at in items],
            )
        conn.commit()
    finally:
        conn.close()
