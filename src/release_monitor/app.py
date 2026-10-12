"""The poll cycle: discover releases, apply filters, wait for notes, queue and deliver.

Per source the first cycle only *records* what already exists (the baseline); announcements
start with versions first seen afterwards. Filters are applied to those new versions only, so
changing ``MINECRAFT_SNAPSHOTS``, ``NEOFORGE_BETAS`` or ``NEOFORGE_MIN_MAJOR`` later can never
replay history. A new release whose notes are not published yet is *deferred* until they appear
or ``CHANGELOG_GRACE_MINUTES`` have passed, and announcements for one source are queued oldest
first.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from release_monitor import __version__
from release_monitor.config import Config
from release_monitor.dispatcher import (
    Dispatcher,
    DispatcherSettings,
    DispatchSummary,
    encode_payload,
)
from release_monitor.models import Origin, Release, ReleaseStatus, Source, StoredRelease
from release_monitor.notifier.formatting import format_minecraft, format_neoforge
from release_monitor.notifier.telegram import TelegramClient
from release_monitor.sources import minecraft, neoforge
from release_monitor.sources.base import ReleaseDetails, SourceError
from release_monitor.sources.minecraft import MinecraftSource
from release_monitor.sources.neoforge import NeoForgeSource
from release_monitor.storage.repository import DuplicateNotificationError, Repository
from release_monitor.timeutil import utc_now

LOG = logging.getLogger(__name__)

USER_AGENT = (
    f"MinecraftNeoForgeTelegramMonitor/{__version__} "
    "(+https://github.com/athegreat90/minecraft-neoforge-telegram-monitor)"
)

Provider = Callable[[Release], ReleaseDetails]


@dataclass(frozen=True, slots=True)
class SourceReport:
    """What one source did during a cycle."""

    source: Source
    ok: bool
    baselined: int = 0
    discovered: int = 0
    queued: int = 0
    deferred: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CycleReport:
    sources: tuple[SourceReport, ...]
    delivery: DispatchSummary


def _unavailable(reason: str) -> ReleaseDetails:
    return ReleaseDetails(False, reason=reason)


class Monitor:
    """Runs poll cycles against both sources and the outbox."""

    def __init__(
        self,
        config: Config,
        repository: Repository,
        minecraft_source: MinecraftSource,
        neoforge_source: NeoForgeSource,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], datetime] = utc_now,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._repo = repository
        self._minecraft = minecraft_source
        self._neoforge = neoforge_source
        self._dispatcher = dispatcher
        self._clock = clock
        self._heartbeat = heartbeat
        self._tz = ZoneInfo(config.display_timezone)
        self._mc_ids: set[str] = set()

    @property
    def repository(self) -> Repository:
        return self._repo

    def set_heartbeat(self, heartbeat: Callable[[], None] | None) -> None:
        self._heartbeat = heartbeat

    # -- lifecycle ----------------------------------------------------------------------

    def recover_on_start(self) -> None:
        """Resolve notifications a previous process left half-sent."""
        recovered = self._dispatcher.recover_on_start()
        if recovered:
            LOG.warning(
                "resolved %d notification(s) left in 'sending' by a previous run (policy: %s)",
                len(recovered),
                self._config.ambiguous_policy,
            )

    def run_forever(self, stop: threading.Event, *, interval: float | None = None) -> None:
        """Poll until ``stop`` is set; a failing cycle never ends the loop."""
        wait = float(self._config.poll_seconds if interval is None else interval)
        LOG.info("monitor started: polling every %ds", int(wait))
        self._beat()  # alive before the first, possibly slow, cycle
        while not stop.is_set():
            try:
                self.run_cycle(stop)
            except Exception:
                LOG.exception("poll cycle failed")
            self._beat()
            if stop.wait(wait):
                break
        LOG.info("shutting down")

    def _beat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except OSError as exc:
            LOG.warning("could not write the heartbeat: %s", type(exc).__name__)

    # -- one cycle ----------------------------------------------------------------------

    def run_cycle(self, stop: threading.Event | None = None) -> CycleReport:
        """Poll both sources (isolated from each other), then deliver whatever is due."""
        reports = [self._guarded(Source.MINECRAFT, self._poll_minecraft)]
        if stop is None or not stop.is_set():
            reports.append(self._guarded(Source.NEOFORGE, self._poll_neoforge))
        delivery = self._deliver()
        self._repo.set_last_poll(self._clock())
        report = CycleReport(tuple(reports), delivery)
        _log_cycle(report)
        return report

    def _guarded(self, source: Source, poll: Callable[[], SourceReport]) -> SourceReport:
        try:
            return poll()
        except SourceError as exc:
            LOG.error("%s: could not poll: %s", source.value, exc)
            return SourceReport(source, False, error=str(exc))
        except Exception as exc:  # one source's bug or outage must not stop the other
            LOG.exception("unexpected error while polling %s", source.value)
            return SourceReport(source, False, error=type(exc).__name__)

    def _deliver(self) -> DispatchSummary:
        try:
            return self._dispatcher.run_due()
        except Exception:
            LOG.exception("delivering queued notifications failed")
            return DispatchSummary()

    def _poll_minecraft(self) -> SourceReport:
        releases = self._fetch(Source.MINECRAFT)
        return self._poll(Source.MINECRAFT, releases, minecraft.is_eligible, self._mc_provider)

    def _poll_neoforge(self) -> SourceReport:
        releases = self._fetch(Source.NEOFORGE)
        return self._poll(Source.NEOFORGE, releases, neoforge.is_eligible, self._nf_provider)

    def _fetch(self, source: Source) -> list[Release]:
        """Every classified release of ``source``, oldest first."""
        if source is Source.MINECRAFT:
            releases = self._minecraft.fetch_all()
            self._mc_ids = {item.version for item in releases}
        else:
            releases = self._neoforge.fetch_all(self._known_mc_ids())
        if not releases:
            raise SourceError(f"{source.value} returned no releases")
        return releases

    def _known_mc_ids(self) -> set[str]:
        """Minecraft versions used to verify NeoForge compatibility claims."""
        return self._mc_ids or self._repo.known_versions(Source.MINECRAFT)

    def _poll(
        self,
        source: Source,
        releases: list[Release],
        eligible: Callable[[Release, Config], bool],
        make_provider: Callable[[Sequence[Release]], Provider],
    ) -> SourceReport:
        repo, now = self._repo, self._clock()
        if not repo.is_initialized(source):
            count = repo.baseline(source, releases)
            repo.set_last_success(source, now)
            LOG.info(
                "%s: first run, recorded %d existing version(s) and announced none",
                source.value,
                count,
            )
            return SourceReport(source, True, baselined=count)

        known = repo.known_versions(source)
        discovered = 0
        for release in releases:
            if release.version in known:
                continue
            is_wanted = eligible(release, self._config)
            repo.add_discovered(
                release, ReleaseStatus.DEFERRED if is_wanted else ReleaseStatus.FILTERED
            )
            discovered += 1
            LOG.info(
                "%s: new version %s (%s)%s",
                source.value,
                release.version,
                release.kind.value,
                "" if is_wanted else " - filtered out by configuration",
            )
        repo.set_last_success(source, now)
        queued, deferred = self._queue_ready(source, releases, make_provider)
        return SourceReport(source, True, discovered=discovered, queued=queued, deferred=deferred)

    def _queue_ready(
        self,
        source: Source,
        fetched: list[Release],
        make_provider: Callable[[Sequence[Release]], Provider],
    ) -> tuple[int, int]:
        """Queue deferred releases whose notes are ready (or whose grace period ended)."""
        order = {item.version: index for index, item in enumerate(fetched)}
        waiting = sorted(
            self._repo.deferred_releases(source),
            key=lambda item: (order.get(item.release.version, -1), item.first_seen_at),
        )
        if not waiting:
            return 0, 0
        provide = make_provider([item.release for item in waiting])
        grace = timedelta(minutes=self._config.changelog_grace_minutes)
        now = self._clock()
        queued = 0
        for index, stored in enumerate(waiting):
            details = provide(stored.release)
            if not details.ready and now - stored.first_seen_at < grace:
                held = len(waiting) - index
                LOG.info(
                    "%s %s: waiting for the official notes (%s); %d announcement(s) held back",
                    source.value,
                    stored.release.version,
                    details.reason or "not published yet",
                    held,
                )
                return queued, held  # keep the oldest-first order: nothing newer goes ahead
            self._queue(stored, details, Origin.LIVE)
            queued += 1
        return queued, 0

    def _queue(self, stored: StoredRelease, details: ReleaseDetails, origin: Origin) -> None:
        release = stored.release
        if release.source is Source.NEOFORGE:
            release = self._with_verified_compatibility(release)
            parts = format_neoforge(release, details, self._tz, detected_at=stored.first_seen_at)
        else:
            parts = format_minecraft(release, details, self._tz, detected_at=stored.first_seen_at)
        try:
            self._repo.enqueue(release.source, release.version, encode_payload(parts), origin)
        except DuplicateNotificationError:
            self._repo.set_release_status(release.source, release.version, ReleaseStatus.QUEUED)
            return
        if not details.ready:
            LOG.warning(
                "%s %s: announcing without notes (%s)",
                release.source.value,
                release.version,
                details.reason or "not published",
            )

    def _with_verified_compatibility(self, release: Release) -> Release:
        """Verify the Minecraft version late, in case the manifest only now lists it."""
        if release.mc_version is not None:
            return release
        parsed = neoforge.parse_version(release.version)
        candidate = parsed.minecraft_version if parsed else None
        if candidate is not None and candidate in self._known_mc_ids():
            return replace(release, mc_version=candidate)
        return release

    # -- details providers ----------------------------------------------------------------

    def _mc_provider(self, releases: Sequence[Release]) -> Provider:
        try:
            batch = self._minecraft.fetch_details(releases)
        except Exception:
            LOG.exception("could not read the Minecraft release notes")
            batch = {}
        return lambda release: (
            batch.get(release.version) or _unavailable("the release notes could not be read")
        )

    def _nf_provider(self, releases: Sequence[Release]) -> Provider:
        del releases  # NeoForge notes are fetched per build

        def provide(release: Release) -> ReleaseDetails:
            try:
                return self._neoforge.fetch_details(release)
            except Exception:
                LOG.exception("could not read the NeoForge changelog for %s", release.version)
                return _unavailable("the changelog could not be read")

        return provide

    # -- operator commands --------------------------------------------------------------

    def rebaseline(self, source: Source) -> int:
        """Record every upstream version as known without announcing anything."""
        return self._repo.baseline(source, self._fetch(source))

    def backfill(
        self, source: Source, *, since: str | None, limit: int, execute: bool
    ) -> list[Release]:
        """Announce past releases on request; returns the candidates, oldest first.

        The window is the newest ``limit`` releases that pass the current filters and are not
        older than ``since``. Versions already queued are dropped from it afterwards, so
        repeating a command never posts anything twice and never walks further back.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        releases = self._fetch(source)
        position = {item.version: index for index, item in enumerate(releases)}
        wanted = minecraft.is_eligible if source is Source.MINECRAFT else neoforge.is_eligible
        if since is not None and since not in position:
            raise ValueError(f"version {since!r} not found among the {source.value} releases")
        start = position[since] if since is not None else 0
        window = [
            item
            for item in releases
            if position[item.version] >= start and wanted(item, self._config)
        ][-limit:]
        candidates = [
            item for item in window if not self._repo.has_notification(source, item.version)
        ]
        if not execute or not candidates:
            return candidates

        provide = (self._mc_provider if source is Source.MINECRAFT else self._nf_provider)(
            candidates
        )
        for release in candidates:
            self._repo.add_discovered(release, ReleaseStatus.DEFERRED)
            stored = self._repo.get_release(source, release.version)
            if stored is None:  # pragma: no cover - add_discovered just inserted it
                continue
            self._queue(replace(stored, release=release), provide(release), Origin.BACKFILL)
        self._deliver()
        return candidates


def _log_cycle(report: CycleReport) -> None:
    changes = sum(item.discovered + item.queued for item in report.sources)
    delivery = report.delivery
    if changes or delivery.sent or delivery.retried or delivery.failed:
        LOG.info(
            "cycle done: %d new, %d queued, %d sent, %d to retry, %d failed",
            sum(item.discovered for item in report.sources),
            sum(item.queued for item in report.sources),
            delivery.sent,
            delivery.retried,
            delivery.failed,
        )
    else:
        LOG.debug("cycle done: nothing to announce")


def build_monitor(
    config: Config,
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat: Callable[[], None] | None = None,
) -> Monitor:
    """Wire the sources, the Telegram client and the outbox around one database."""
    repository = Repository(conn, clock)
    dispatcher = Dispatcher(
        repository,
        TelegramClient(config.token, client),
        {
            Source.MINECRAFT: config.minecraft_channel_id,
            Source.NEOFORGE: config.neoforge_channel_id,
        },
        settings=DispatcherSettings(ambiguous_policy=config.ambiguous_policy),
        clock=clock,
        sleep=sleep,
    )
    return Monitor(
        config,
        repository,
        MinecraftSource(client, sleep=sleep),
        NeoForgeSource(client, sleep=sleep),
        dispatcher,
        clock=clock,
        heartbeat=heartbeat,
    )
