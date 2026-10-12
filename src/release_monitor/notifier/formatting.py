"""Build Telegram HTML messages from structured data.

Every dynamic value is escaped and every line is self-contained HTML (tags opened and closed on
the same line), so a message can be split on line boundaries without ever producing a broken
tag. Rendered HTML is never sliced: text is truncated *before* it is escaped.
"""

import html
from collections.abc import Sequence
from datetime import datetime, tzinfo

from release_monitor.models import PublishedAtKind, Release, ReleaseKind
from release_monitor.sources.base import ReleaseDetails

TELEGRAM_LIMIT = 4096
DEFAULT_SPLIT_LIMIT = 3900
MAX_BULLETS = 8
MAX_BULLET_CHARS = 300
DATE_FORMAT = "%d %b %Y, %H:%M %Z"

_MINECRAFT_TYPE = {
    ReleaseKind.STABLE: "Stable",
    ReleaseKind.SNAPSHOT: "Snapshot",
    ReleaseKind.PRERELEASE: "Pre-release",
    ReleaseKind.RELEASE_CANDIDATE: "Release candidate",
}
_NEOFORGE_STATUS = {
    ReleaseKind.STABLE: "Stable",
    ReleaseKind.BETA: "Beta",
    ReleaseKind.ALPHA: "Alpha",
}
_DATE_LABEL = {
    PublishedAtKind.OFFICIAL: "Released",
    PublishedAtKind.ARTIFACT: "Artifact timestamp",
    PublishedAtKind.DETECTED: "Detected",
}


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def truncate(text: str, limit: int) -> str:
    """Shorten plain text to ``limit`` characters, ending with an ellipsis if it was cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _field(label: str, value: str) -> str:
    return f"<b>{esc(label)}:</b> {esc(value)}"


def _moment(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime(DATE_FORMAT)


def _date_line(release: Release, details: ReleaseDetails, tz: tzinfo, detected_at: datetime) -> str:
    """Label the timestamp by what it really is; never present a guess as a release date."""
    if release.published_at is not None:
        moment, kind = release.published_at, release.published_at_kind
    elif details.published_at is not None:
        moment, kind = details.published_at, details.published_at_kind
    else:
        moment, kind = detected_at, PublishedAtKind.DETECTED
    return "📅 " + _field(_DATE_LABEL[kind], _moment(moment, tz))


def _changelog_block(details: ReleaseDetails, what: str) -> str:
    lines = ["📋 <b>Changelog</b>"]
    if not details.ready:
        lines.append(f"⏳ The official {what} is not available yet; see the link below.")
        return "\n".join(lines)
    if not details.highlights:
        lines.append(f"📝 No summary available; see the full {what}.")
        return "\n".join(lines)
    shown = details.highlights[:MAX_BULLETS]
    lines.extend(f"• {esc(truncate(item, MAX_BULLET_CHARS))}" for item in shown)
    hidden = len(details.highlights) - len(shown)
    if hidden > 0:
        lines.append(f"…and {hidden} more")
    return "\n".join(lines)


def _link_block(details: ReleaseDetails) -> str:
    url = details.official_url
    if not url:
        return ""
    return f'🔗 <a href="{esc(url)}">{esc(details.link_label)}</a>'


def split_message(blocks: Sequence[str], limit: int = DEFAULT_SPLIT_LIMIT) -> list[str]:
    """Pack blocks (joined by a blank line) into parts of at most ``limit`` characters.

    A block larger than ``limit`` is split on line boundaries. A single line larger than
    ``limit`` cannot be split safely and raises ``ValueError``.
    """
    parts: list[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        for chunk in _fit(block, limit):
            if not current:
                current = chunk
            elif len(current) + 2 + len(chunk) <= limit:
                current = f"{current}\n\n{chunk}"
            else:
                parts.append(current)
                current = chunk
    if current:
        parts.append(current)
    return parts


def _fit(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    chunks: list[str] = []
    current = ""
    for line in block.split("\n"):
        if len(line) > limit:
            raise ValueError(f"line too long to split safely ({len(line)} > {limit})")
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            chunks.append(current)
            current = line
    chunks.append(current)
    return chunks


def format_minecraft(
    release: Release, details: ReleaseDetails, tz: tzinfo, *, detected_at: datetime
) -> list[str]:
    """The announcement for a Minecraft Java Edition release, as one or more message parts."""
    info = "\n".join(
        [
            _field("Version", release.version),
            _field("Type", _MINECRAFT_TYPE.get(release.kind, release.kind.value.title())),
            _date_line(release, details, tz, detected_at),
        ]
    )
    return split_message(
        [
            "⛏️ <b>Minecraft Java Edition Update</b>",
            info,
            _changelog_block(details, "release notes"),
            _link_block(details),
        ]
    )


def format_neoforge(
    release: Release, details: ReleaseDetails, tz: tzinfo, *, detected_at: datetime
) -> list[str]:
    """The announcement for a NeoForge build, as one or more message parts."""
    info = "\n".join(
        [
            _field("Version", release.version),
            _field("Minecraft compatibility", release.mc_version or "unverified"),
            _field(
                "Release status", _NEOFORGE_STATUS.get(release.kind, release.kind.value.title())
            ),
            _date_line(release, details, tz, detected_at),
        ]
    )
    return split_message(
        [
            "🛠️ <b>NeoForge Update</b>",
            info,
            _changelog_block(details, "changelog"),
            _link_block(details),
        ]
    )
