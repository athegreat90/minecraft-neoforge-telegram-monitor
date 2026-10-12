"""Minecraft Java Edition: Mojang's version manifest plus the official patch-notes feed."""

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx

from release_monitor.config import Config
from release_monitor.models import PublishedAtKind, Release, ReleaseKind, Source
from release_monitor.sources.base import Fetcher, ReleaseDetails, SourceError

LOG = logging.getLogger(__name__)

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PATCH_NOTES_BASE = "https://launchercontent.mojang.com/v2/"
PATCH_NOTES_FEED_URL = PATCH_NOTES_BASE + "javaPatchNotes.json"
ARTICLE_BASE = "https://www.minecraft.net/en-us/article/"
NEWS_URL = "https://www.minecraft.net/en-us/articles"

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_NOTES_BYTES = 4 * 1024 * 1024
MAX_HIGHLIGHTS = 5

_PRE_RE = re.compile(r"^(?P<base>.+)-pre-?(?P<n>\d+)$")
_RC_RE = re.compile(r"^(?P<base>.+)-rc-?(?P<n>\d+)$")
_LEGACY_SNAPSHOT_RE = re.compile(r"^\d{2}w\d{2}[a-z]$")
_CONTENT_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


def classify(version_id: str, mojang_type: str) -> ReleaseKind | None:
    """Map a manifest entry to a release kind; ``None`` means "never announce"."""
    if mojang_type == "release":
        return ReleaseKind.STABLE
    if mojang_type != "snapshot":
        return None  # old_alpha / old_beta
    if _PRE_RE.match(version_id):
        return ReleaseKind.PRERELEASE
    if _RC_RE.match(version_id):
        return ReleaseKind.RELEASE_CANDIDATE
    return ReleaseKind.SNAPSHOT


def parse_manifest(data: object) -> list[Release]:
    """Turn a ``version_manifest_v2.json`` document into releases, oldest first."""
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        raise SourceError("Mojang manifest is malformed: no 'versions' list")
    releases: list[Release] = []
    for entry in data["versions"]:
        release = _parse_entry(entry)
        if release is not None:
            releases.append(release)
    if not releases:
        raise SourceError("Mojang manifest contains no usable versions")
    oldest = datetime.min.replace(tzinfo=UTC)
    releases.sort(key=lambda item: (item.published_at or oldest, item.version))
    return releases


def _parse_entry(entry: object) -> Release | None:
    if not isinstance(entry, dict):
        return None
    version, mojang_type, stamp = entry.get("id"), entry.get("type"), entry.get("releaseTime")
    if not (isinstance(version, str) and isinstance(mojang_type, str) and isinstance(stamp, str)):
        LOG.debug("skipping manifest entry without id/type/releaseTime: %r", entry)
        return None
    kind = classify(version, mojang_type)
    if kind is None:
        return None
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError:
        LOG.warning("skipping %s: unparseable releaseTime %r", version, stamp)
        return None
    if published.tzinfo is None:
        return None
    return Release(
        source=Source.MINECRAFT,
        version=version,
        kind=kind,
        published_at=published,
        published_at_kind=PublishedAtKind.OFFICIAL,
    )


def is_eligible(release: Release, config: Config) -> bool:
    """Whether ``release`` should be announced under the current filters."""
    match release.kind:
        case ReleaseKind.STABLE:
            return True
        case ReleaseKind.SNAPSHOT:
            return config.minecraft_snapshots
        case ReleaseKind.PRERELEASE | ReleaseKind.RELEASE_CANDIDATE:
            return config.minecraft_prereleases
        case _:
            return False


# -- patch-notes HTML ---------------------------------------------------------------------

_SECTION_RANK = (
    ("new features", 0),
    ("changes", 1),
    ("fixes", 2),
    ("fixed bugs", 2),
    ("bug fixes", 2),
    ("technical changes", 3),
)


def _section_rank(title: str) -> int | None:
    lowered = title.strip().lower()
    for prefix, rank in _SECTION_RANK:
        if lowered == prefix or lowered.startswith(prefix + " "):
            return rank
    return None


class _HighlightParser(HTMLParser):
    """Collects the top-level list items directly under each interesting ``<h1>``.

    Mojang's bodies leave ``<li>`` and ``<p>`` unclosed, so item text ends at the next
    ``<li>``, a nested list or the closing list tag. Anything under an ``<h2>`` or deeper is
    detail, not a highlight.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: dict[int, list[str]] = {}
        self._rank: int | None = None
        self._heading: list[str] | None = None
        self._in_subsection = False
        self._depth = 0
        self._item: list[str] | None = None

    def _collecting(self) -> bool:
        return self._rank is not None and not self._in_subsection

    def _flush(self) -> None:
        if self._item is not None and self._rank is not None:
            text = " ".join("".join(self._item).split())
            if text:
                self.sections.setdefault(self._rank, []).append(text)
        self._item = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._depth = 0
            if tag == "h1":
                self._heading = []
                self._in_subsection = False
            else:
                self._in_subsection = True
        elif tag in {"ul", "ol"}:
            self._flush()
            self._depth += 1
        elif tag == "li" and self._depth == 1:
            self._flush()
            if self._collecting():
                self._item = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._heading is not None:
            self._rank = _section_rank("".join(self._heading))
            self._heading = None
        elif tag in {"ul", "ol"}:
            self._flush()
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)
        elif self._item is not None:
            self._item.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_highlights(body_html: str, limit: int = MAX_HIGHLIGHTS) -> list[str]:
    """Pick up to ``limit`` highlights: new features first, then changes, then fixes."""
    parser = _HighlightParser()
    parser.feed(body_html)
    parser.close()
    seen: set[str] = set()
    highlights: list[str] = []
    for rank in sorted(parser.sections):
        for text in parser.sections[rank]:
            if text not in seen:
                seen.add(text)
                highlights.append(text)
    return highlights[:limit]


# -- official article URLs -----------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def article_slugs(version: str, kind: ReleaseKind) -> list[str]:
    """Candidate minecraft.net article slugs, in the patterns observed for 26.x releases."""
    match kind:
        case ReleaseKind.STABLE:
            return [f"minecraft-java-edition-{_slug(version)}"]
        case ReleaseKind.RELEASE_CANDIDATE:
            found = _RC_RE.match(version)
            if found:
                return [f"minecraft-{_slug(found['base'])}-release-candidate-{found['n']}"]
        case ReleaseKind.PRERELEASE:
            found = _PRE_RE.match(version)
            if found:
                return [f"minecraft-{_slug(found['base'])}-pre-release-{found['n']}"]
        case ReleaseKind.SNAPSHOT:
            slug = _slug(version)
            if _LEGACY_SNAPSHOT_RE.match(version):
                return [f"minecraft-snapshot-{slug}"]
            return [f"minecraft-{slug}", f"minecraft-snapshot-{slug}"]
        case _:
            pass
    return []


class MinecraftSource:
    """Reads the manifest and, for new releases, the official patch notes."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self._fetcher = Fetcher(client, sleep=sleep, max_attempts=max_attempts)

    def fetch_all(self) -> list[Release]:
        """Every classified release (filters are applied later, never at baseline time)."""
        response = self._fetcher.get(MANIFEST_URL, max_bytes=MAX_MANIFEST_BYTES)
        return parse_manifest(_json(response, "Mojang manifest"))

    def fetch_details(self, releases: Sequence[Release]) -> dict[str, ReleaseDetails]:
        """Release notes for a batch of releases; the patch-notes feed is fetched once."""
        if not releases:
            return {}
        try:
            entries = self._feed_entries()
            feed_error: str | None = None
        except SourceError as exc:
            LOG.warning("patch-notes feed unavailable: %s", exc)
            entries, feed_error = {}, f"the official patch-notes feed is unavailable ({exc})"
        return {
            release.version: self._details_for(release, entries, feed_error) for release in releases
        }

    def _feed_entries(self) -> dict[str, dict[str, object]]:
        response = self._fetcher.get(PATCH_NOTES_FEED_URL, max_bytes=MAX_FEED_BYTES)
        feed = _json(response, "patch-notes feed")
        entries = feed.get("entries") if isinstance(feed, dict) else None
        if not isinstance(entries, list):
            raise SourceError("patch-notes feed is malformed: no 'entries' list")
        return {
            entry["version"]: entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("version"), str)
        }

    def _details_for(
        self,
        release: Release,
        entries: dict[str, dict[str, object]],
        feed_error: str | None,
    ) -> ReleaseDetails:
        article = self._confirmed_article(release)
        url = article or NEWS_URL
        label = "Official release notes" if article else "Minecraft news"
        if feed_error is not None:
            return ReleaseDetails(False, notes_url=url, link_label=label, reason=feed_error)
        entry = entries.get(release.version)
        if entry is None:
            return ReleaseDetails(
                False,
                notes_url=url,
                link_label=label,
                reason="the release notes are not in the official patch-notes feed yet",
            )
        try:
            highlights = self._highlights(entry)
        except SourceError as exc:
            LOG.info("patch notes for %s unavailable: %s", release.version, exc)
            return ReleaseDetails(
                False,
                notes_url=url,
                link_label=label,
                reason=f"the official patch notes could not be read ({exc})",
            )
        return ReleaseDetails(True, tuple(highlights), notes_url=url, link_label=label)

    def _highlights(self, entry: dict[str, object]) -> list[str]:
        path = entry.get("contentPath")
        if not isinstance(path, str) or not _CONTENT_PATH_RE.match(path) or ".." in path:
            raise SourceError("the feed entry has no usable content path")
        response = self._fetcher.get(PATCH_NOTES_BASE + path, max_bytes=MAX_NOTES_BYTES)
        document = _json(response, "patch notes")
        body = document.get("body") if isinstance(document, dict) else None
        if not isinstance(body, str):
            raise SourceError("patch notes have no body")
        return extract_highlights(body)

    def _confirmed_article(self, release: Release) -> str | None:
        """Return the article URL only if minecraft.net answers 200 for it."""
        for slug in article_slugs(release.version, release.kind):
            url = ARTICLE_BASE + slug
            try:
                with self._fetcher.client.stream("GET", url) as response:
                    if response.status_code == httpx.codes.OK:
                        return url
            except httpx.HTTPError as exc:
                LOG.info("could not verify %s: %s", url, type(exc).__name__)
        return None


def _json(response: httpx.Response, what: str) -> object:
    try:
        return json.loads(response.content)
    except ValueError:
        raise SourceError(f"{what} is not valid JSON") from None
