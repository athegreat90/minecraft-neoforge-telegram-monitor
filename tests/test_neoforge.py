"""NeoForge source: Maven metadata, version ordering, compatibility and changelogs."""

from datetime import UTC, datetime

import httpx
import pytest

from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source
from release_monitor.sources import neoforge
from release_monitor.sources.base import SourceError
from release_monitor.sources.neoforge import (
    METADATA_URL,
    NeoForgeSource,
    changelog_url,
    extract_changelog_entry,
    is_eligible,
    parse_metadata,
    parse_version,
    pom_url,
)
from tests.fixtures_util import FakeWeb, fixture_text, make_config, no_sleep

KNOWN_MC = {"26.1", "26.1.2", "26.2", "26.3"}


def make_source(web: FakeWeb) -> NeoForgeSource:
    return NeoForgeSource(web.client(), sleep=no_sleep, max_attempts=2)


def metadata_xml(*versions: str) -> str:
    items = "".join(f"<version>{v}</version>" for v in versions)
    return (
        "<metadata><groupId>net.neoforged</groupId><artifactId>neoforge</artifactId>"
        f"<versioning><versions>{items}</versions></versioning></metadata>"
    )


def stored(version: str, kind: ReleaseKind = ReleaseKind.STABLE) -> Release:
    return Release(source=Source.NEOFORGE, version=version, kind=kind)


# -- version parsing and ordering -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("26.3.0.76", ReleaseKind.STABLE),
        ("26.3.0.75-beta", ReleaseKind.BETA),
        ("26.1.0.0-alpha.1+snapshot-1", ReleaseKind.ALPHA),
        ("26.1.0.0-alpha.15+pre-3", ReleaseKind.ALPHA),
        ("21.1.259", ReleaseKind.STABLE),
        ("27.1.0.1", ReleaseKind.STABLE),
    ],
)
def test_parse_version_accepts_the_real_schemes(raw: str, kind: ReleaseKind) -> None:
    parsed = parse_version(raw)
    assert parsed is not None
    assert parsed.kind is kind


@pytest.mark.parametrize(
    "raw", ["0.25w14craftmine.3-beta", "", "26.3", "26.3.0.76-rc.1", "abc", "26.3.0.76-beta2"]
)
def test_parse_version_rejects_unknown_schemes(raw: str) -> None:
    assert parse_version(raw) is None


def test_legacy_three_segment_versions_parse_but_have_no_minecraft_version() -> None:
    legacy = parse_version("21.1.259")
    modern = parse_version("26.1.2.116")
    assert legacy is not None
    assert modern is not None
    assert legacy.minecraft_version is None
    assert modern.minecraft_version == "26.1.2"
    assert (legacy.major, legacy.minor, legacy.build) == (21, 1, 259)
    newer = parse_version("21.1.1000")
    assert newer is not None
    assert legacy < newer


def test_versions_sort_numerically_and_by_stage_not_lexically() -> None:
    raws = [
        "26.1.0.1-beta",
        "26.1.0.0-alpha.15+pre-3",
        "26.1.0.0-alpha.2+snapshot-1",
        "26.1.0.0-alpha.10+snapshot-6",
        "26.1.0.0-alpha.9+snapshot-5",
        "26.1.0.10-beta",
        "26.1.0.9-beta",
        "26.1.2.116",
        "26.1.0.100",
    ]
    parsed = [parse_version(raw) for raw in raws]
    assert None not in parsed
    ordered = [item.raw for item in sorted(item for item in parsed if item is not None)]
    assert ordered == [
        "26.1.0.0-alpha.2+snapshot-1",
        "26.1.0.0-alpha.9+snapshot-5",
        "26.1.0.0-alpha.10+snapshot-6",
        "26.1.0.0-alpha.15+pre-3",
        "26.1.0.1-beta",
        "26.1.0.9-beta",
        "26.1.0.10-beta",
        "26.1.0.100",
        "26.1.2.116",
    ]


# -- metadata ---------------------------------------------------------------------------


def test_parse_metadata_sorts_the_unordered_real_document() -> None:
    """G4 regression: the live Maven document is NOT in publication order."""
    text = fixture_text("neoforge-maven-metadata.xml")
    document_order = [part.split("</version>")[0] for part in text.split("<version>")[1:]]
    assert document_order.index("26.2.0.90") > document_order.index("26.3.0.66-beta")

    versions = [item.version for item in parse_metadata(text, KNOWN_MC)]

    assert versions.index("26.2.0.90") < versions.index("26.3.0.66-beta")
    assert versions.index("26.3.0.66-beta") < versions.index("26.3.0.67-beta")
    assert versions[-1] == "26.3.0.76"
    assert versions[0].startswith("21.1.")


def test_parse_metadata_classifies_and_skips_unknown_ids() -> None:
    releases = {
        item.version: item
        for item in parse_metadata(fixture_text("neoforge-maven-metadata.xml"), KNOWN_MC)
    }
    assert "0.25w14craftmine.3-beta" not in releases
    assert releases["26.1.0.0-alpha.1+snapshot-1"].kind is ReleaseKind.ALPHA
    assert releases["26.1.0.0-alpha.15+pre-3"].kind is ReleaseKind.ALPHA
    assert releases["26.3.0.75-beta"].kind is ReleaseKind.BETA
    assert releases["26.3.0.76"].kind is ReleaseKind.STABLE
    assert releases["26.3.0.76"].source is Source.NEOFORGE
    assert releases["26.3.0.76"].published_at is None
    assert releases["26.3.0.76"].published_at_kind is PublishedAtKind.DETECTED
    assert releases["26.3.0.76"].url == changelog_url("26.3.0.76")


def test_mc_version_is_set_only_when_verified_against_known_versions() -> None:
    text = fixture_text("neoforge-maven-metadata.xml")
    verified = {item.version: item.mc_version for item in parse_metadata(text, KNOWN_MC)}
    assert verified["26.3.0.76"] == "26.3"
    assert verified["26.1.2.116"] == "26.1.2"
    assert verified["26.2.0.90"] == "26.2"

    unverified = {item.version: item.mc_version for item in parse_metadata(text, {"26.3"})}
    assert unverified["26.3.0.76"] == "26.3"
    assert unverified["26.1.2.116"] is None

    nothing_known = {item.version: item.mc_version for item in parse_metadata(text, ())}
    assert set(nothing_known.values()) == {None}


def test_parse_metadata_rejects_bad_documents() -> None:
    with pytest.raises(SourceError, match="malformed"):
        parse_metadata("<metadata><versions>", ())
    with pytest.raises(SourceError, match="no versions"):
        parse_metadata(metadata_xml(), ())
    with pytest.raises(SourceError, match="no versions"):
        parse_metadata(metadata_xml("0.25w14craftmine.3-beta"), ())


def test_parse_metadata_ignores_duplicates() -> None:
    result = parse_metadata(metadata_xml("26.3.0.76", "26.3.0.76", "26.3.0.75-beta"), KNOWN_MC)
    assert [item.version for item in result] == ["26.3.0.75-beta", "26.3.0.76"]


def test_fetch_all_returns_everything_parseable_regardless_of_filters() -> None:
    web = FakeWeb()
    web.add(METADATA_URL, text=fixture_text("neoforge-maven-metadata.xml"))
    versions = [item.version for item in make_source(web).fetch_all(KNOWN_MC)]
    assert "21.1.258" in versions
    assert "26.1.0.0-alpha.1+snapshot-1" in versions
    assert "26.3.0.76" in versions


def test_fetch_all_reports_failures_as_source_errors() -> None:
    web = FakeWeb()
    web.add(METADATA_URL, status=500)
    with pytest.raises(SourceError):
        make_source(web).fetch_all()

    web = FakeWeb()
    web.add(METADATA_URL, error=httpx.ReadTimeout("slow"))
    with pytest.raises(SourceError):
        make_source(web).fetch_all()


def test_fetch_all_rejects_oversized_responses() -> None:
    web = FakeWeb()
    web.add(METADATA_URL, content=b"<" * (neoforge.MAX_METADATA_BYTES + 1))
    with pytest.raises(SourceError, match="too large"):
        make_source(web).fetch_all()


# -- eligibility ------------------------------------------------------------------------


def test_is_eligible_applies_min_major_and_beta_switch() -> None:
    with_betas = make_config(neoforge_betas=True, neoforge_min_major=26)
    without_betas = make_config(neoforge_betas=False, neoforge_min_major=26)

    assert is_eligible(stored("26.3.0.76"), with_betas)
    assert is_eligible(stored("26.3.0.75-beta", ReleaseKind.BETA), with_betas)
    assert is_eligible(stored("26.1.0.0-alpha.1+snapshot-1", ReleaseKind.ALPHA), with_betas)
    assert is_eligible(stored("26.3.0.76"), without_betas)
    assert not is_eligible(stored("26.3.0.75-beta", ReleaseKind.BETA), without_betas)
    assert not is_eligible(stored("26.1.0.0-alpha.1+snapshot-1", ReleaseKind.ALPHA), without_betas)
    assert not is_eligible(stored("21.1.258"), with_betas)


def test_future_major_versions_are_picked_up_automatically() -> None:
    config = make_config(neoforge_min_major=26)
    releases = parse_metadata(metadata_xml("26.3.0.76", "27.1.0.1", "27.1.0.2-beta"), KNOWN_MC)
    eligible = [item.version for item in releases if is_eligible(item, config)]
    assert eligible == ["26.3.0.76", "27.1.0.1", "27.1.0.2-beta"]
    assert releases[-1].mc_version is None  # 27.1 is not a known Minecraft version


def test_is_eligible_rejects_unparseable_versions() -> None:
    assert not is_eligible(stored("garbage"), make_config())


def test_multiple_new_builds_come_out_oldest_first() -> None:
    ordered = [
        item.version
        for item in parse_metadata(fixture_text("neoforge-maven-metadata.xml"), KNOWN_MC)
    ]
    known = set(ordered[: ordered.index("26.3.0.73-beta") + 1])
    fresh = [version for version in ordered if version not in known]
    assert fresh == ["26.3.0.74-beta", "26.3.0.75-beta", "26.3.0.76"]


# -- changelog --------------------------------------------------------------------------


def test_extract_changelog_entry_returns_only_the_announced_build() -> None:
    text = fixture_text("neoforge-26.3.0.76-changelog.txt")
    assert extract_changelog_entry(text, "26.3.0.76") == [
        "Change changelog base tag to 26.3.0 (#3635)"
    ]
    assert extract_changelog_entry(text, "26.3.0.75-beta") == [
        "Add builder bifunction variant for EntityType DR registration (#2770)"
    ]


def test_extract_changelog_entry_drops_credit_lines_but_keeps_real_continuations() -> None:
    text = fixture_text("neoforge-26.3.0.76-changelog.txt")
    assert extract_changelog_entry(text, "26.3.0.74-beta") == [
        "Refactor AbstractWidget#getFGColor as getLabelTextColor (#3063)"
    ]

    custom = (
        " - `1.0.0.2` First line (#1)\n"
        "   More detail here\n"
        "   Signed-off-by: x\n"
        " - `1.0.0.1` Older\n"
    )
    assert extract_changelog_entry(custom, "1.0.0.2") == ["First line (#1)", "More detail here"]


def test_extract_changelog_entry_returns_none_when_the_build_is_absent() -> None:
    text = fixture_text("neoforge-26.3.0.76-changelog.txt")
    assert extract_changelog_entry(text, "26.3.0.77") is None
    assert extract_changelog_entry("", "26.3.0.76") is None


def test_artifact_urls_encode_plus_signs() -> None:
    version = "26.1.0.0-alpha.1+snapshot-1"
    assert "%2B" in changelog_url(version)
    assert "+" not in changelog_url(version)
    assert changelog_url("26.3.0.76") == (
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/26.3.0.76/"
        "neoforge-26.3.0.76-changelog.txt"
    )
    assert pom_url("26.3.0.76").endswith("/26.3.0.76/neoforge-26.3.0.76.pom")


# -- details ----------------------------------------------------------------------------

LAST_MODIFIED = "Sun, 11 Oct 2026 19:46:02 GMT"


def details_web(*, changelog: bool = True, pom: bool = True) -> FakeWeb:
    web = FakeWeb()
    if changelog:
        web.add(changelog_url("26.3.0.76"), text=fixture_text("neoforge-26.3.0.76-changelog.txt"))
    if pom:
        web.add(pom_url("26.3.0.76"), method="HEAD", headers={"Last-Modified": LAST_MODIFIED})
    return web


def test_fetch_details_returns_changelog_and_artifact_timestamp() -> None:
    details = make_source(details_web()).fetch_details(stored("26.3.0.76"))

    assert details.ready is True
    assert details.highlights == ("Change changelog base tag to 26.3.0 (#3635)",)
    assert details.changelog_url == changelog_url("26.3.0.76")
    assert details.link_label == "Official changelog"
    assert details.published_at == datetime(2026, 10, 11, 19, 46, 2, tzinfo=UTC)
    assert details.published_at_kind is PublishedAtKind.ARTIFACT


def test_fetch_details_with_a_missing_changelog_is_not_ready() -> None:
    details = make_source(details_web(changelog=False)).fetch_details(stored("26.3.0.76"))

    assert details.ready is False
    assert details.highlights == ()
    assert details.reason is not None
    assert details.changelog_url == changelog_url("26.3.0.76")
    assert details.published_at_kind is PublishedAtKind.ARTIFACT  # the pom is still there


def test_fetch_details_when_the_changelog_lags_behind_the_build_is_not_ready() -> None:
    web = FakeWeb()
    web.add(changelog_url("26.3.0.77"), text=fixture_text("neoforge-26.3.0.76-changelog.txt"))
    details = make_source(web).fetch_details(stored("26.3.0.77"))
    assert details.ready is False
    assert details.reason is not None


def test_fetch_details_without_a_usable_timestamp_leaves_it_unset() -> None:
    details = make_source(details_web(pom=False)).fetch_details(stored("26.3.0.76"))
    assert details.ready is True
    assert details.published_at is None
    assert details.published_at_kind is PublishedAtKind.DETECTED

    web = details_web(pom=False)
    web.add(pom_url("26.3.0.76"), method="HEAD", headers={"Last-Modified": "garbage"})
    assert make_source(web).fetch_details(stored("26.3.0.76")).published_at is None


def test_fetch_details_treats_network_errors_as_not_ready() -> None:
    web = FakeWeb()
    web.add(changelog_url("26.3.0.76"), error=httpx.ConnectError("down"))
    details = make_source(web).fetch_details(stored("26.3.0.76"))
    assert details.ready is False
    assert details.reason
