"""NeoForge: Maven metadata for discovery and per-build changelogs for the announcement."""

import email.utils
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from release_monitor.config import Config
from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source
from release_monitor.sources.base import Fetcher, ReleaseDetails, SourceError

LOG = logging.getLogger(__name__)

BASE_URL = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
METADATA_URL = f"{BASE_URL}/maven-metadata.xml"
MAX_METADATA_BYTES = 5 * 1024 * 1024
MAX_CHANGELOG_BYTES = 2 * 1024 * 1024

# MAJOR.MINOR.PATCH.BUILD, optionally "-beta" or "-alpha.N+snapshot-M" / "-alpha.N+pre-M".
_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:\.(?P<build>\d+))?"
    r"(?:(?P<beta>-beta)|-alpha\.(?P<alpha>\d+)\+(?P<tag>snapshot|pre)-(?P<tag_n>\d+))?$"
)
_ENTRY_RE = re.compile(r"^\s*-\s+`(?P<version>[^`]+)`\s*(?P<text>.*)$")
_CREDIT_PREFIXES = ("co-authored-by:", "signed-off-by:")

_STAGE_ALPHA, _STAGE_BETA, _STAGE_STABLE = 0, 1, 2
_TAG_RANK = {"snapshot": 0, "pre": 1}


@dataclass(frozen=True, slots=True, order=True)
class ParsedVersion:
    """A NeoForge version whose natural ordering is the publication order.

    Field order is the sort order: Minecraft version, build, stage (alpha < beta < stable),
    then the alpha counter and its snapshot/pre-release tag.
    """

    major: int
    minor: int
    patch: int
    build: int
    stage: int
    alpha: int
    tag_rank: int
    tag_n: int
    raw: str = field(compare=False)
    legacy: bool = field(default=False, compare=False)

    @property
    def kind(self) -> ReleaseKind:
        if self.stage == _STAGE_ALPHA:
            return ReleaseKind.ALPHA
        if self.stage == _STAGE_BETA:
            return ReleaseKind.BETA
        return ReleaseKind.STABLE

    @property
    def minecraft_version(self) -> str | None:
        """The Minecraft version this build targets, per NeoForge's 26.x+ scheme.

        ``None`` for the older ``MAJOR.MINOR.BUILD`` scheme (e.g. ``21.1.259``), which drops the
        leading "1" of the Minecraft version and is not worth guessing at.
        """
        if self.legacy:
            return None
        base = f"{self.major}.{self.minor}"
        return base if self.patch == 0 else f"{base}.{self.patch}"


def parse_version(raw: str) -> ParsedVersion | None:
    """Parse a NeoForge version id; ``None`` for ids outside the known schemes."""
    found = _VERSION_RE.match(raw)
    if found is None:
        return None
    if found["beta"]:
        stage, alpha, tag_rank, tag_n = _STAGE_BETA, 0, 0, 0
    elif found["alpha"]:
        stage, alpha = _STAGE_ALPHA, int(found["alpha"])
        tag_rank, tag_n = _TAG_RANK[found["tag"]], int(found["tag_n"])
    else:
        stage, alpha, tag_rank, tag_n = _STAGE_STABLE, 0, 0, 0
    legacy = found["build"] is None
    return ParsedVersion(
        int(found["major"]),
        int(found["minor"]),
        0 if legacy else int(found["patch"]),
        int(found["patch"] if legacy else found["build"]),
        stage,
        alpha,
        tag_rank,
        tag_n,
        raw,
        legacy,
    )


def _quoted(version: str) -> str:
    return urllib.parse.quote(version, safe=".-_")


def artifact_url(version: str, suffix: str) -> str:
    quoted = _quoted(version)
    return f"{BASE_URL}/{quoted}/neoforge-{quoted}{suffix}"


def changelog_url(version: str) -> str:
    return artifact_url(version, "-changelog.txt")


def pom_url(version: str) -> str:
    return artifact_url(version, ".pom")


def parse_metadata(text: str, known_mc_versions: Collection[str] = ()) -> list[Release]:
    """Parse ``maven-metadata.xml`` into releases sorted oldest first.

    The document order is NOT chronological, so the result is always re-sorted. A release
    gets ``mc_version`` only when the derived Minecraft version is in ``known_mc_versions``.
    """
    try:
        # Safe enough: the document comes from the official repo over HTTPS and is size-capped
        # by the fetcher; the stdlib parser does not resolve external entities.
        root = ET.fromstring(text)  # noqa: S314
    except ET.ParseError as exc:
        raise SourceError(f"NeoForge metadata is malformed: {exc}") from None
    known = frozenset(known_mc_versions)
    parsed: dict[str, ParsedVersion] = {}
    for element in root.iter("version"):
        raw = (element.text or "").strip()
        version = parse_version(raw)
        if version is None:
            if raw:
                LOG.debug("skipping NeoForge version with unknown scheme: %s", raw)
            continue
        parsed.setdefault(raw, version)
    if not parsed:
        raise SourceError("NeoForge metadata lists no versions with a known scheme")
    return [
        Release(
            source=Source.NEOFORGE,
            version=item.raw,
            kind=item.kind,
            mc_version=_verified(item, known),
            url=changelog_url(item.raw),
        )
        for item in sorted(parsed.values())
    ]


def _verified(item: ParsedVersion, known: frozenset[str]) -> str | None:
    candidate = item.minecraft_version
    return candidate if candidate is not None and candidate in known else None


def is_eligible(release: Release, config: Config) -> bool:
    """Whether ``release`` should be announced under the current filters."""
    parsed = parse_version(release.version)
    if parsed is None or parsed.major < config.neoforge_min_major:
        return False
    return config.neoforge_betas or parsed.kind is ReleaseKind.STABLE


def extract_changelog_entry(text: str, version: str) -> list[str] | None:
    """Return the lines of ``version``'s own entry from the cumulative changelog.

    The file is newest-first with one `` - `<version>` text`` bullet per build; indented
    lines continue the entry. Credit trailers are dropped. ``None`` if the build is absent.
    """
    lines: list[str] = []
    collecting = False
    for line in text.splitlines():
        entry = _ENTRY_RE.match(line)
        if entry:
            if collecting:
                break
            if entry["version"] == version:
                collecting = True
                if entry["text"].strip():
                    lines.append(entry["text"].strip())
            continue
        if not collecting:
            continue
        stripped = line.strip()
        if not stripped or set(stripped) == {"-"} or stripped.lower().startswith(_CREDIT_PREFIXES):
            continue
        lines.append(stripped)
    return lines if collecting else None


class NeoForgeSource:
    """Reads the Maven metadata and, for new builds, the changelog and artifact timestamp."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self._fetcher = Fetcher(client, sleep=sleep, max_attempts=max_attempts)

    def fetch_all(self, known_mc_versions: Collection[str] = ()) -> list[Release]:
        """Every parseable version (the baseline records all of them, filters apply later)."""
        response = self._fetcher.get(METADATA_URL, max_bytes=MAX_METADATA_BYTES)
        return parse_metadata(response.text, known_mc_versions)

    def fetch_details(self, release: Release) -> ReleaseDetails:
        """The build's own changelog entry and its artifact timestamp, when available."""
        version = release.version
        url = changelog_url(version)
        published_at, kind = self._artifact_time(version)

        def details(highlights: tuple[str, ...], reason: str | None) -> ReleaseDetails:
            return ReleaseDetails(
                ready=reason is None,
                highlights=highlights,
                changelog_url=url,
                link_label="Official changelog",
                published_at=published_at,
                published_at_kind=kind,
                reason=reason,
            )

        try:
            text = self._fetcher.get(url, max_bytes=MAX_CHANGELOG_BYTES).text
        except SourceError as exc:
            if exc.status_code == httpx.codes.NOT_FOUND:
                return details((), "the changelog has not been published yet")
            return details((), f"the changelog could not be read ({exc})")
        entry = extract_changelog_entry(text, version)
        if entry is None:
            return details((), "the changelog does not list this build yet")
        return details(tuple(entry), None)

    def _artifact_time(self, version: str) -> tuple[datetime | None, PublishedAtKind]:
        """Last-Modified of the build's ``.pom``.

        The pom is the first file Maven receives for a build (observed: seconds before the
        changelog and minutes before the installer) and always exists, so it is the closest
        reliable proxy for "when the build appeared". It is an artifact timestamp, not an
        official release date, and is labelled as such.
        """
        try:
            header = self._fetcher.head(pom_url(version)).headers.get("Last-Modified")
        except SourceError as exc:
            LOG.info("no artifact timestamp for %s: %s", version, exc)
            return None, PublishedAtKind.DETECTED
        if not header:
            return None, PublishedAtKind.DETECTED
        try:
            moment = email.utils.parsedate_to_datetime(header)
        except TypeError, ValueError:
            return None, PublishedAtKind.DETECTED
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC), PublishedAtKind.ARTIFACT
