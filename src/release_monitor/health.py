"""Container health from local state only: a heartbeat file and recent successful polls.

The check never touches the network, so a Telegram or upstream outage cannot make the
container look dead on its own, and it never prints configuration or secrets.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from release_monitor.models import Source
from release_monitor.timeutil import from_iso, to_iso, utc_now

HEARTBEAT_NAME = "heartbeat"
HEARTBEAT_SLACK_SECONDS = 60
HEARTBEAT_CYCLES = 3
SUCCESS_CYCLES = 6
DB_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    reason: str


def heartbeat_path(db_path: Path) -> Path:
    return db_path.parent / HEARTBEAT_NAME


def write_heartbeat(db_path: Path, now: datetime | None = None) -> None:
    """Atomically record that the poll loop is alive."""
    target = heartbeat_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(HEARTBEAT_NAME + ".tmp")
    scratch.write_text(to_iso(now or utc_now()), encoding="utf-8")
    scratch.replace(target)


def check_health(db_path: Path, poll_seconds: int, now: datetime | None = None) -> HealthStatus:
    """Healthy when the loop beats regularly and at least one source polled recently."""
    moment = now or utc_now()
    beat = _heartbeat_age(db_path, moment)
    if isinstance(beat, HealthStatus):
        return beat
    limit = HEARTBEAT_CYCLES * poll_seconds + HEARTBEAT_SLACK_SECONDS
    if beat.total_seconds() > limit:
        return HealthStatus(False, f"heartbeat is stale ({int(beat.total_seconds())}s old)")

    successes = _last_successes(db_path)
    if isinstance(successes, HealthStatus):
        return successes
    recent = {
        name: moment - when
        for name, when in successes.items()
        if moment - when <= timedelta(seconds=SUCCESS_CYCLES * poll_seconds)
    }
    if not recent:
        return HealthStatus(
            False, f"no source was polled successfully in the last {SUCCESS_CYCLES * poll_seconds}s"
        )
    detail = ", ".join(
        f"{name} {int(age.total_seconds())}s ago" for name, age in sorted(recent.items())
    )
    return HealthStatus(True, f"ok (last successful poll: {detail})")


def _heartbeat_age(db_path: Path, now: datetime) -> timedelta | HealthStatus:
    path = heartbeat_path(db_path)
    try:
        stamp = from_iso(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return HealthStatus(False, "no heartbeat yet")
    except OSError, ValueError:
        return HealthStatus(False, "heartbeat file is unreadable")
    return now - stamp


def _last_successes(db_path: Path) -> dict[str, datetime] | HealthStatus:
    if not db_path.exists():
        return HealthStatus(False, "database file does not exist")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=DB_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        return HealthStatus(False, f"database cannot be opened read-only ({type(exc).__name__})")
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'last_success:%'"
        ).fetchall()
    except sqlite3.Error as exc:
        return HealthStatus(False, f"database cannot be read ({type(exc).__name__})")
    finally:
        conn.close()
    found: dict[str, datetime] = {}
    known = {source.value for source in Source}
    for key, value in rows:
        name = str(key).removeprefix("last_success:")
        if name in known:
            try:
                found[name] = from_iso(str(value))
            except ValueError:
                continue
    return found
