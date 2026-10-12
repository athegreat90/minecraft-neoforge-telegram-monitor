"""UTC helpers. Timestamps are persisted as fixed-width ISO-8601 strings so they sort correctly."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC)."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def from_iso(text: str) -> datetime:
    """Parse a stored timestamp. Naive values (very old data) are assumed to be UTC."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
