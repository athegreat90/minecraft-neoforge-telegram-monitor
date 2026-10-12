"""Minecraft source: manifest parsing, filtering and official release notes."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source
from release_monitor.sources import minecraft
from release_monitor.sources.base import SourceError
from release_monitor.sources.minecraft import (
    MANIFEST_URL,
    NEWS_URL,
    PATCH_NOTES_FEED_URL,
    MinecraftSource,
    classify,
    extract_highlights,
    is_eligible,
    parse_manifest,
)
from tests.fixtures_util import FakeWeb, fixture_json, fixture_text, make_config, no_sleep

NOTES_BASE = "https://launchercontent.mojang.com/v2/"
ARTICLE = "https://www.minecraft.net/en-us/article/"


def make_source(web: FakeWeb) -> MinecraftSource:
    return MinecraftSource(web.client(), sleep=no_sleep, max_attempts=2)


def release(version: str, kind: ReleaseKind) -> Release:
    return Release(source=Source.MINECRAFT, version=version, kind=kind)


@pytest.mark.parametrize(
    ("version_id", "mojang_type", "expected"),
    [
        ("26.3", "release", ReleaseKind.STABLE),
        ("1.21.1", "release", ReleaseKind.STABLE),
        ("26.3-pre-1", "snapshot", ReleaseKind.PRERELEASE),
        ("1.20.5-pre1", "snapshot", ReleaseKind.PRERELEASE),
        ("26.3-rc-3", "snapshot", ReleaseKind.RELEASE_CANDIDATE),
        ("1.20.5-rc1", "snapshot", ReleaseKind.RELEASE_CANDIDATE),
        ("26.4-snapshot-3", "snapshot", ReleaseKind.SNAPSHOT),
        ("25w46a", "snapshot", ReleaseKind.SNAPSHOT),
        ("b1.7.3", "old_beta", None),
        ("a1.2.6", "old_alpha", None),
    ],
)
def test_classify(version_id: str, mojang_type: str, expected: ReleaseKind | None) -> None:
    assert classify(version_id, mojang_type) == expected


def test_parse_manifest_classifies_and_sorts_oldest_first() -> None:
    releases = parse_manifest(fixture_json("version_manifest_v2.json"))
    by_version = {item.version: item for item in releases}

    assert "b1.7.3" not in by_version
    assert "a1.2.6" not in by_version
    assert by_version["26.3"].kind is ReleaseKind.STABLE
    assert by_version["26.3-rc-3"].kind is ReleaseKind.RELEASE_CANDIDATE
    assert by_version["26.3-pre-1"].kind is ReleaseKind.PRERELEASE
    assert by_version["26.4-snapshot-3"].kind is ReleaseKind.SNAPSHOT
    assert by_version["25w46a"].kind is ReleaseKind.SNAPSHOT

    times = [item.published_at for item in releases]
    assert times == sorted(times)  # type: ignore[type-var]
    assert releases[-1].version == "26.4-snapshot-3"


def test_parse_manifest_uses_the_official_release_time() -> None:
    stable = next(
        item
        for item in parse_manifest(fixture_json("version_manifest_v2.json"))
        if item.version == "26.3"
    )
    assert stable.published_at == datetime(2026, 9, 15, 11, 23, 2, tzinfo=UTC)
    assert stable.published_at_kind is PublishedAtKind.OFFICIAL
    assert stable.source is Source.MINECRAFT


@pytest.mark.parametrize("bad", [None, [], {}, {"versions": "nope"}, {"versions": []}])
def test_parse_manifest_rejects_malformed_documents(bad: object) -> None:
    with pytest.raises(SourceError):
        parse_manifest(bad)


def test_parse_manifest_skips_entries_without_usable_fields() -> None:
    data = {
        "versions": [
            {"id": "26.3", "type": "release", "releaseTime": "2026-09-15T11:23:02+00:00"},
            {"id": "26.9", "type": "release", "releaseTime": "not-a-date"},
            {"type": "release"},
            "junk",
        ]
    }
    assert [item.version for item in parse_manifest(data)] == ["26.3"]


@pytest.mark.parametrize(
    ("kind", "snapshots", "prereleases", "expected"),
    [
        (ReleaseKind.STABLE, False, False, True),
        (ReleaseKind.SNAPSHOT, False, False, False),
        (ReleaseKind.SNAPSHOT, True, False, True),
        (ReleaseKind.PRERELEASE, True, False, False),
        (ReleaseKind.PRERELEASE, False, True, True),
        (ReleaseKind.RELEASE_CANDIDATE, False, True, True),
        (ReleaseKind.RELEASE_CANDIDATE, True, False, False),
    ],
)
def test_is_eligible(kind: ReleaseKind, snapshots: bool, prereleases: bool, expected: bool) -> None:
    config = make_config(minecraft_snapshots=snapshots, minecraft_prereleases=prereleases)
    assert is_eligible(release("26.3", kind), config) is expected


def test_fetch_all_returns_every_classified_release_regardless_of_filters() -> None:
    web = FakeWeb()
    web.add(MANIFEST_URL, text=fixture_text("version_manifest_v2.json"))
    releases = make_source(web).fetch_all()
    kinds = {item.kind for item in releases}
    assert kinds == {
        ReleaseKind.STABLE,
        ReleaseKind.SNAPSHOT,
        ReleaseKind.PRERELEASE,
        ReleaseKind.RELEASE_CANDIDATE,
    }


def test_fetch_all_reports_http_and_json_failures_as_source_errors() -> None:
    web = FakeWeb()
    web.add(MANIFEST_URL, status=503)
    with pytest.raises(SourceError):
        make_source(web).fetch_all()

    web = FakeWeb()
    web.add(MANIFEST_URL, text="<html>not json</html>")
    with pytest.raises(SourceError):
        make_source(web).fetch_all()

    web = FakeWeb()
    web.add(MANIFEST_URL, error=httpx.ConnectError("boom"))
    with pytest.raises(SourceError):
        make_source(web).fetch_all()


def test_fetch_all_rejects_oversized_responses() -> None:
    web = FakeWeb()
    web.add(MANIFEST_URL, content=b"x" * (minecraft.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(SourceError, match="too large"):
        make_source(web).fetch_all()


# -- highlights -----------------------------------------------------------------------


def test_extract_highlights_from_the_real_patch_notes_body() -> None:
    body = fixture_json("patch_notes_26-3.json")["body"]
    highlights = extract_highlights(body)
    assert highlights == [
        "Added Dappled Forest Biome",
        "Added Poplar Trees",
        "Added fallen Poplar Trees",
        "Added Red Poplar Leaves, Orange Poplar Leaves, and Yellow Poplar Leaves blocks",
        "Added Poplar wood set",
    ]


def test_extract_highlights_handles_unclosed_items_and_ignores_nested_lists() -> None:
    html = (
        "<p>intro<h1>Changes</h1><ul><li>One<li>Two <code>x</code> done"
        "<ul><li>nested</ul><li>Three</ul>"
    )
    assert extract_highlights(html) == ["One", "Two x done", "Three"]


def test_extract_highlights_prefers_features_then_changes_then_fixes() -> None:
    html = (
        "<h1>Fixes</h1><ul><li>Fixed a crash</ul>"
        "<h1>Changes</h1><ul><li>Changed a thing</ul>"
        "<h1>New Features</h1><ul><li>Added a thing</ul>"
    )
    assert extract_highlights(html) == ["Added a thing", "Changed a thing", "Fixed a crash"]


def test_extract_highlights_respects_the_limit_and_skips_unknown_sections() -> None:
    items = "".join(f"<li>Added {n}" for n in range(9))
    html = f"<h1>Credits</h1><ul><li>Somebody</ul><h1>New Features</h1><ul>{items}</ul>"
    result = extract_highlights(html, limit=3)
    assert result == ["Added 0", "Added 1", "Added 2"]


def test_extract_highlights_returns_nothing_for_bodies_without_lists() -> None:
    assert extract_highlights("<p>Just a story.</p>") == []


# -- release notes --------------------------------------------------------------------


def notes_web(*, article_ok: tuple[str, ...] = ("minecraft-java-edition-26-3",)) -> FakeWeb:
    web = FakeWeb()
    web.add(PATCH_NOTES_FEED_URL, text=fixture_text("java_patch_notes.json"))
    body = fixture_text("patch_notes_26-3.json")
    web.add(NOTES_BASE + "javaPatchNotes/26-3.json", text=body)
    web.add(NOTES_BASE + "javaPatchNotes/26-3-release-candidate-3.json", text=body)
    for slug in article_ok:
        web.add(ARTICLE + slug, text="<html></html>")
    return web


def test_fetch_details_for_a_stable_release_uses_feed_and_verified_article() -> None:
    web = notes_web()
    details = make_source(web).fetch_details([release("26.3", ReleaseKind.STABLE)])["26.3"]

    assert details.ready is True
    assert len(details.highlights) == 5
    assert details.highlights[0] == "Added Dappled Forest Biome"
    assert details.notes_url == ARTICLE + "minecraft-java-edition-26-3"
    assert details.link_label == "Official release notes"
    assert details.reason is None


def test_fetch_details_falls_back_to_news_when_no_article_is_confirmed() -> None:
    web = notes_web(article_ok=())
    details = make_source(web).fetch_details([release("26.3", ReleaseKind.STABLE)])["26.3"]

    assert details.ready is True
    assert details.notes_url == NEWS_URL
    assert details.link_label == "Minecraft news"


def test_fetch_details_knows_the_real_article_slug_patterns() -> None:
    web = notes_web(article_ok=("minecraft-26-3-release-candidate-3",))
    rc = release("26.3-rc-3", ReleaseKind.RELEASE_CANDIDATE)
    details = make_source(web).fetch_details([rc])["26.3-rc-3"]
    assert details.notes_url == ARTICLE + "minecraft-26-3-release-candidate-3"

    web = notes_web(article_ok=("minecraft-26-3-pre-release-1",))
    pre = release("26.3-pre-1", ReleaseKind.PRERELEASE)
    assert (
        make_source(web).fetch_details([pre])["26.3-pre-1"].notes_url
        == ARTICLE + "minecraft-26-3-pre-release-1"
    )

    web = notes_web(article_ok=("minecraft-26-4-snapshot-3",))
    snap = release("26.4-snapshot-3", ReleaseKind.SNAPSHOT)
    assert (
        make_source(web).fetch_details([snap])["26.4-snapshot-3"].notes_url
        == ARTICLE + "minecraft-26-4-snapshot-3"
    )

    web = notes_web(article_ok=("minecraft-snapshot-25w46a",))
    legacy = release("25w46a", ReleaseKind.SNAPSHOT)
    assert (
        make_source(web).fetch_details([legacy])["25w46a"].notes_url
        == ARTICLE + "minecraft-snapshot-25w46a"
    )


def test_fetch_details_without_a_feed_entry_is_not_ready_and_invents_nothing() -> None:
    web = notes_web()
    details = make_source(web).fetch_details([release("26.9", ReleaseKind.STABLE)])["26.9"]

    assert details.ready is False
    assert details.highlights == ()
    assert details.reason is not None
    assert details.notes_url == NEWS_URL


def test_fetch_details_with_a_missing_body_is_not_ready() -> None:
    web = notes_web()
    web.add(NOTES_BASE + "javaPatchNotes/26-3.json", status=404)
    details = make_source(web).fetch_details([release("26.3", ReleaseKind.STABLE)])["26.3"]

    assert details.ready is False
    assert details.reason is not None
    assert details.highlights == ()


def test_fetch_details_survives_a_feed_outage() -> None:
    web = FakeWeb()
    web.add(PATCH_NOTES_FEED_URL, status=503)
    result = make_source(web).fetch_details(
        [release("26.3", ReleaseKind.STABLE), release("26.3-rc-3", ReleaseKind.RELEASE_CANDIDATE)]
    )
    assert {item.ready for item in result.values()} == {False}
    assert set(result) == {"26.3", "26.3-rc-3"}


def test_fetch_details_fetches_the_feed_once_per_batch() -> None:
    web = notes_web()
    batch = [
        release("26.3", ReleaseKind.STABLE),
        release("26.3-rc-3", ReleaseKind.RELEASE_CANDIDATE),
    ]
    make_source(web).fetch_details(batch)
    assert web.count(PATCH_NOTES_FEED_URL) == 1


def test_fetch_details_with_no_releases_makes_no_requests() -> None:
    web = notes_web()
    assert make_source(web).fetch_details([]) == {}
    assert web.requests == []


def test_fetch_details_refuses_suspicious_content_paths() -> None:
    feed = fixture_json("java_patch_notes.json")
    feed["entries"][1]["contentPath"] = "../../etc/passwd"
    web = FakeWeb()
    web.add(PATCH_NOTES_FEED_URL, text=json.dumps(feed))
    details = make_source(web).fetch_details([release("26.3", ReleaseKind.STABLE)])["26.3"]
    assert details.ready is False
    assert all("passwd" not in str(request.url) for request in web.requests)
