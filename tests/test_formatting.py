"""Message formatting: escaping, labelled dates and tag-safe splitting."""

from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source
from release_monitor.notifier import formatting
from release_monitor.notifier.formatting import (
    TELEGRAM_LIMIT,
    format_minecraft,
    format_neoforge,
    split_message,
)
from release_monitor.sources.base import ReleaseDetails

UTC_TZ = ZoneInfo("UTC")
MADRID = ZoneInfo("Europe/Madrid")
DETECTED = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
RELEASED = datetime(2026, 9, 15, 11, 23, 2, tzinfo=UTC)
ARTICLE = "https://www.minecraft.net/en-us/article/minecraft-java-edition-26-3"
CHANGELOG = "https://maven.neoforged.net/releases/net/neoforged/neoforge/26.3.0.76/c.txt"


class BalanceChecker(HTMLParser):
    """Fails on any unbalanced or unknown tag, mirroring what Telegram would reject."""

    ALLOWED: ClassVar[set[str]] = {"b", "i", "a", "code", "pre", "u", "s"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.ALLOWED:
            self.problems.append(f"unexpected <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.problems.append(f"unbalanced </{tag}>")


def assert_valid_html(part: str) -> None:
    checker = BalanceChecker()
    checker.feed(part)
    checker.close()
    assert checker.problems == []
    assert checker.stack == []
    assert len(part) <= TELEGRAM_LIMIT


def mc_release(kind: ReleaseKind = ReleaseKind.STABLE, version: str = "26.3") -> Release:
    return Release(
        source=Source.MINECRAFT,
        version=version,
        kind=kind,
        published_at=RELEASED,
        published_at_kind=PublishedAtKind.OFFICIAL,
    )


def nf_release(mc_version: str | None = "26.3", kind: ReleaseKind = ReleaseKind.STABLE) -> Release:
    return Release(
        source=Source.NEOFORGE, version="26.3.0.76", kind=kind, mc_version=mc_version, url=CHANGELOG
    )


def ready_details(
    *highlights: str,
    changelog_url: str | None = None,
    notes_url: str | None = None,
    link_label: str = "Official release notes",
    published_at: datetime | None = None,
    published_at_kind: PublishedAtKind = PublishedAtKind.DETECTED,
) -> ReleaseDetails:
    return ReleaseDetails(
        True,
        highlights,
        changelog_url=changelog_url,
        notes_url=notes_url,
        link_label=link_label,
        published_at=published_at,
        published_at_kind=published_at_kind,
    )


# -- Minecraft ------------------------------------------------------------------------


def test_minecraft_message_layout() -> None:
    details = ready_details("Added Poplar Trees", "Added Cushions", notes_url=ARTICLE)
    (message,) = format_minecraft(mc_release(), details, UTC_TZ, detected_at=DETECTED)

    assert "⛏️ <b>Minecraft Java Edition Update</b>" in message
    assert "<b>Version:</b> 26.3" in message
    assert "<b>Type:</b> Stable" in message
    assert "<b>Released:</b> 15 Sep 2026, 11:23 UTC" in message
    assert "• Added Poplar Trees" in message
    assert "• Added Cushions" in message
    assert f'<a href="{ARTICLE}">Official release notes</a>' in message
    assert_valid_html(message)


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        (ReleaseKind.STABLE, "Stable"),
        (ReleaseKind.SNAPSHOT, "Snapshot"),
        (ReleaseKind.PRERELEASE, "Pre-release"),
        (ReleaseKind.RELEASE_CANDIDATE, "Release candidate"),
    ],
)
def test_minecraft_type_labels(kind: ReleaseKind, label: str) -> None:
    (message,) = format_minecraft(
        mc_release(kind), ready_details("x", notes_url=ARTICLE), UTC_TZ, detected_at=DETECTED
    )
    assert f"<b>Type:</b> {label}" in message


def test_minecraft_message_without_ready_notes_says_so_and_still_links() -> None:
    details = ReleaseDetails(
        False,
        notes_url="https://www.minecraft.net/en-us/articles",
        link_label="Minecraft news",
        reason="the release notes are not in the official patch-notes feed yet",
    )
    (message,) = format_minecraft(mc_release(), details, UTC_TZ, detected_at=DETECTED)

    assert "not available yet" in message
    assert "• " not in message
    assert '<a href="https://www.minecraft.net/en-us/articles">Minecraft news</a>' in message
    assert_valid_html(message)


# -- NeoForge -------------------------------------------------------------------------


def test_neoforge_message_layout_with_verified_compatibility_and_artifact_time() -> None:
    stamp = datetime(2026, 10, 11, 19, 46, 2, tzinfo=UTC)
    details = ready_details(
        "Change changelog base tag to 26.3.0 (#3635)",
        changelog_url=CHANGELOG,
        link_label="Official changelog",
        published_at=stamp,
        published_at_kind=PublishedAtKind.ARTIFACT,
    )
    (message,) = format_neoforge(nf_release(), details, UTC_TZ, detected_at=DETECTED)

    assert "🛠️ <b>NeoForge Update</b>" in message
    assert "<b>Version:</b> 26.3.0.76" in message
    assert "<b>Minecraft compatibility:</b> 26.3" in message
    assert "<b>Release status:</b> Stable" in message
    assert "<b>Artifact timestamp:</b> 11 Oct 2026, 19:46 UTC" in message
    assert "• Change changelog base tag to 26.3.0 (#3635)" in message
    assert f'<a href="{CHANGELOG}">Official changelog</a>' in message
    assert_valid_html(message)


def test_neoforge_unverified_compatibility_is_labelled_not_guessed() -> None:
    details = ready_details("x", changelog_url=CHANGELOG, link_label="Official changelog")
    (message,) = format_neoforge(nf_release(mc_version=None), details, UTC_TZ, detected_at=DETECTED)
    assert "<b>Minecraft compatibility:</b> unverified" in message


@pytest.mark.parametrize(
    ("kind", "label"),
    [(ReleaseKind.BETA, "Beta"), (ReleaseKind.ALPHA, "Alpha"), (ReleaseKind.STABLE, "Stable")],
)
def test_neoforge_release_status_labels(kind: ReleaseKind, label: str) -> None:
    details = ready_details("x", changelog_url=CHANGELOG, link_label="Official changelog")
    (message,) = format_neoforge(nf_release(kind=kind), details, UTC_TZ, detected_at=DETECTED)
    assert f"<b>Release status:</b> {label}" in message


# -- dates ------------------------------------------------------------------------------


def test_missing_timestamp_is_labelled_as_detection_time_never_as_a_release_date() -> None:
    details = ready_details("x", changelog_url=CHANGELOG, link_label="Official changelog")
    (message,) = format_neoforge(nf_release(), details, UTC_TZ, detected_at=DETECTED)

    assert "<b>Detected:</b> 12 Oct 2026, 09:30 UTC" in message
    assert "Released" not in message
    assert "Artifact timestamp" not in message


def test_dates_are_rendered_in_the_display_timezone() -> None:
    details = ready_details("x", notes_url=ARTICLE)
    (message,) = format_minecraft(mc_release(), details, MADRID, detected_at=DETECTED)
    assert "<b>Released:</b> 15 Sep 2026, 13:23 CEST" in message


# -- escaping and truncation -----------------------------------------------------------


def test_every_dynamic_value_is_html_escaped() -> None:
    nasty = '<script>alert("x")</script> & <b>bold</b>'
    release = Release(
        source=Source.MINECRAFT, version="26.3<i>", kind=ReleaseKind.STABLE, published_at=RELEASED
    )
    details = ReleaseDetails(
        True,
        (nasty,),
        notes_url='https://example.test/?a=1&b="2"',
        link_label="<Notes> & more",
    )
    (message,) = format_minecraft(release, details, UTC_TZ, detected_at=DETECTED)

    assert "<script>" not in message
    assert (
        "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in message
    )
    assert "26.3&lt;i&gt;" in message
    assert 'href="https://example.test/?a=1&amp;b=&quot;2&quot;"' in message
    assert "&lt;Notes&gt; &amp; more" in message
    assert_valid_html(message)


def test_bullets_are_truncated_before_escaping_so_entities_are_never_cut() -> None:
    details = ready_details("&" * 1000, notes_url=ARTICLE)
    (message,) = format_minecraft(mc_release(), details, UTC_TZ, detected_at=DETECTED)

    bullet = next(line for line in message.splitlines() if line.startswith("• "))
    assert bullet.endswith("…")
    assert bullet.count("&amp;") == formatting.MAX_BULLET_CHARS - 1
    assert "&am…" not in message
    assert_valid_html(message)


def test_extra_bullets_are_summarised() -> None:
    items = [f"Change {n}" for n in range(formatting.MAX_BULLETS + 4)]
    details = ready_details(*items, notes_url=ARTICLE)
    (message,) = format_minecraft(mc_release(), details, UTC_TZ, detected_at=DETECTED)

    assert message.count("• Change") == formatting.MAX_BULLETS
    assert "…and 4 more" in message


def test_a_huge_changelog_still_yields_valid_parts_within_the_limit() -> None:
    details = ready_details(*["<&>" * 200] * 200, notes_url=ARTICLE)
    parts = format_minecraft(mc_release(), details, UTC_TZ, detected_at=DETECTED)
    assert parts
    for part in parts:
        assert_valid_html(part)


# -- split_message ----------------------------------------------------------------------


def test_split_message_keeps_short_messages_whole() -> None:
    assert split_message(["<b>a</b>", "b"]) == ["<b>a</b>\n\nb"]


def test_split_message_splits_between_blocks_and_never_exceeds_the_limit() -> None:
    blocks = [f"<b>block {n}</b> " + "x" * 1000 for n in range(10)]
    parts = split_message(blocks, limit=3900)

    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 3900
        assert_valid_html(part)
    assert "\n\n".join(parts).replace("\n\n", "") == "".join(blocks)


def test_split_message_splits_an_oversized_block_on_line_boundaries() -> None:
    block = "\n".join(f"• <b>line {n}</b> " + "y" * 90 for n in range(100))
    parts = split_message([block], limit=1000)

    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 1000
        assert_valid_html(part)
    assert "\n".join(parts) == block


def test_split_message_refuses_a_single_line_that_cannot_fit() -> None:
    with pytest.raises(ValueError, match="too long"):
        split_message(["z" * 50], limit=10)


def test_split_message_ignores_empty_blocks() -> None:
    assert split_message(["", "a", ""]) == ["a"]
    assert split_message([]) == []
