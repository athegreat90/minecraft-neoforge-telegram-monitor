"""End-to-end behaviour of the poll cycle against the fake upstream world."""

import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from release_monitor.app import Monitor
from release_monitor.models import NotificationState, Origin, ReleaseStatus, Source
from release_monitor.sources.neoforge import METADATA_URL
from tests.helpers import build_legacy_db
from tests.world import MC_CHAT, NF_CHAT, World


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    instance = World(tmp_path)
    yield instance
    instance.stop()


def started(world: World, **overrides: object) -> Monitor:
    monitor = world.start(**overrides)
    monitor.recover_on_start()
    return monitor


def baseline(world: World, **overrides: object) -> Monitor:
    """First cycle: records history and must post nothing."""
    monitor = started(world, **overrides)
    monitor.run_cycle()
    assert world.messages == []
    return monitor


# -- first run -------------------------------------------------------------------------


def test_first_run_baselines_every_version_and_posts_nothing(world: World) -> None:
    monitor = started(world)

    report = monitor.run_cycle()

    assert world.messages == []
    assert all(item.ok for item in report.sources)
    repo = monitor.repository
    assert repo.is_initialized(Source.MINECRAFT)
    assert repo.is_initialized(Source.NEOFORGE)
    # Everything is recorded, including versions the default filters would never announce.
    assert {"26.3", "26.4-snapshot-3", "26.3-rc-1"} <= repo.known_versions(Source.MINECRAFT)
    assert {"26.3.0.76", "26.3.0.75-beta", "21.1.259"} <= repo.known_versions(Source.NEOFORGE)
    assert repo.last_success(Source.MINECRAFT) == world.clock.now
    assert repo.last_success(Source.NEOFORGE) == world.clock.now
    assert repo.last_poll() == world.clock.now


def test_a_failed_first_fetch_does_not_initialise_the_source(world: World) -> None:
    world.take_down(Source.NEOFORGE)
    monitor = started(world)

    report = monitor.run_cycle()

    by_source = {item.source: item for item in report.sources}
    assert by_source[Source.MINECRAFT].ok
    assert not by_source[Source.NEOFORGE].ok
    assert monitor.repository.is_initialized(Source.MINECRAFT)
    assert not monitor.repository.is_initialized(Source.NEOFORGE)
    assert monitor.repository.last_success(Source.NEOFORGE) is None


# -- announcing new releases ---------------------------------------------------------------


def test_a_new_minecraft_release_is_posted_exactly_once(world: World) -> None:
    monitor = baseline(world)
    world.clock.advance(minutes=5)
    world.add_minecraft("26.3.1")

    monitor.run_cycle()
    monitor.run_cycle()

    texts = world.texts(MC_CHAT)
    assert len(texts) == 1
    assert "Minecraft Java Edition Update" in texts[0]
    assert "<b>Version:</b> 26.3.1" in texts[0]
    assert "Fixed a thing" in texts[0]
    assert "minecraft-java-edition-26-3-1" in texts[0]
    assert world.texts(NF_CHAT) == []
    stored = monitor.repository.get_release(Source.MINECRAFT, "26.3.1")
    assert stored is not None
    assert stored.status is ReleaseStatus.ANNOUNCED


def test_a_release_is_not_posted_again_after_a_restart(world: World) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1")
    monitor.run_cycle()
    assert len(world.messages) == 1

    restarted = started(world)  # same database, new process
    restarted.run_cycle()

    assert len(world.messages) == 1


def test_a_new_neoforge_build_reports_verified_compatibility_and_artifact_time(
    world: World,
) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.77")

    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "NeoForge Update" in text
    assert "<b>Version:</b> 26.3.0.77" in text
    assert "<b>Minecraft compatibility:</b> 26.3" in text
    assert "<b>Release status:</b> Stable" in text
    assert "<b>Artifact timestamp:</b> 12 Oct 2026, 09:30 UTC" in text
    assert "Change shipped in 26.3.0.77" in text
    assert "neoforge-26.3.0.77-changelog.txt" in text


def test_unknown_minecraft_version_is_labelled_unverified(world: World) -> None:
    monitor = baseline(world)
    world.add_neoforge("27.1.0.1")  # Minecraft 27.1 does not exist in the manifest

    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "<b>Minecraft compatibility:</b> unverified" in text


def test_missing_artifact_timestamp_is_labelled_as_detection_time(world: World) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.77", pom=False)

    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "<b>Detected:</b>" in text
    assert "Artifact timestamp" not in text


def test_several_new_builds_are_posted_oldest_first_despite_metadata_order(world: World) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.79", "26.3.0.77", "26.3.0.78")

    monitor.run_cycle()

    versions = [text.split("<b>Version:</b> ")[1].split("\n")[0] for text in world.texts(NF_CHAT)]
    assert versions == ["26.3.0.77", "26.3.0.78", "26.3.0.79"]


def test_one_cycle_announces_both_sources(world: World) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1")
    world.add_neoforge("26.3.0.77")

    monitor.run_cycle()

    assert len(world.texts(MC_CHAT)) == 1
    assert len(world.texts(NF_CHAT)) == 1


# -- filters ----------------------------------------------------------------------------------


def test_enabling_filters_after_the_baseline_never_floods_the_channels(world: World) -> None:
    baseline(world)  # defaults: no snapshots, no pre-releases

    permissive = started(
        world,
        minecraft_snapshots=True,
        minecraft_prereleases=True,
        neoforge_betas=True,
        neoforge_min_major=0,
    )
    permissive.run_cycle()

    assert world.messages == []


def test_filters_apply_only_to_versions_discovered_after_the_baseline(world: World) -> None:
    monitor = baseline(world, minecraft_snapshots=False, neoforge_betas=False)
    world.add_minecraft("26.5-snapshot-1", kind="snapshot", article=False)
    world.add_neoforge("26.3.0.77-beta")

    monitor.run_cycle()

    assert world.messages == []
    repo = monitor.repository
    snapshot = repo.get_release(Source.MINECRAFT, "26.5-snapshot-1")
    beta = repo.get_release(Source.NEOFORGE, "26.3.0.77-beta")
    assert snapshot is not None
    assert beta is not None
    assert snapshot.status is ReleaseStatus.FILTERED
    assert beta.status is ReleaseStatus.FILTERED

    snapshots_on = started(world, minecraft_snapshots=True, neoforge_betas=True)
    snapshots_on.run_cycle()
    assert world.messages == []  # already decided; a toggle never resurrects history

    world.add_minecraft("26.5-snapshot-2", kind="snapshot", article=False)
    snapshots_on.run_cycle()
    assert len(world.texts(MC_CHAT)) == 1
    assert "26.5-snapshot-2" in world.texts(MC_CHAT)[0]


# -- late changelogs --------------------------------------------------------------------------


def test_a_missing_changelog_defers_the_post_until_it_appears(world: World) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.77", changelog=False)

    monitor.run_cycle()
    assert world.messages == []
    stored = monitor.repository.get_release(Source.NEOFORGE, "26.3.0.77")
    assert stored is not None
    assert stored.status is ReleaseStatus.DEFERRED

    world.clock.advance(minutes=10)
    monitor.run_cycle()
    assert world.messages == []  # still inside the 30 minute grace period

    world.publish_changelog("26.3.0.77", "Add the late feature (#9)")
    world.clock.advance(minutes=5)
    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "Add the late feature" in text
    assert "not available yet" not in text
    stored = monitor.repository.get_release(Source.NEOFORGE, "26.3.0.77")
    assert stored is not None
    assert stored.status is ReleaseStatus.ANNOUNCED


def test_after_the_grace_period_the_post_says_the_changelog_is_not_available(
    world: World,
) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.77", changelog=False)
    monitor.run_cycle()
    assert world.messages == []

    world.clock.advance(minutes=31)
    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "not available yet" in text
    assert "neoforge-26.3.0.77-changelog.txt" in text  # still links the official page
    assert "Artifact timestamp" in text


def test_a_zero_minute_grace_period_posts_immediately(world: World) -> None:
    monitor = baseline(world, changelog_grace_minutes=0)
    world.add_neoforge("26.3.0.77", changelog=False)

    monitor.run_cycle()

    (text,) = world.texts(NF_CHAT)
    assert "not available yet" in text


def test_minecraft_release_without_notes_waits_then_posts_a_plain_note(world: World) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1", notes=None, article=False)

    monitor.run_cycle()
    assert world.messages == []

    world.clock.advance(minutes=31)
    monitor.run_cycle()

    (text,) = world.texts(MC_CHAT)
    assert "not available yet" in text
    assert "Fixed a thing" not in text
    assert "minecraft.net/en-us/articles" in text  # generic news link, labelled as news


def test_an_unready_older_build_holds_back_newer_ones_to_keep_the_order(world: World) -> None:
    monitor = baseline(world)
    world.add_neoforge("26.3.0.77", changelog=False)
    world.add_neoforge("26.3.0.78")

    monitor.run_cycle()
    assert world.messages == []

    world.publish_changelog("26.3.0.77")
    monitor.run_cycle()

    versions = [text.split("<b>Version:</b> ")[1].split("\n")[0] for text in world.texts(NF_CHAT)]
    assert versions == ["26.3.0.77", "26.3.0.78"]


# -- outages and recovery ---------------------------------------------------------------------


def test_one_source_outage_does_not_block_the_other(world: World) -> None:
    monitor = baseline(world)
    world.take_down(Source.NEOFORGE)
    world.add_minecraft("26.3.1")
    world.clock.advance(minutes=5)

    report = monitor.run_cycle()

    by_source = {item.source: item for item in report.sources}
    assert by_source[Source.MINECRAFT].ok
    assert not by_source[Source.NEOFORGE].ok
    assert by_source[Source.NEOFORGE].error
    assert len(world.texts(MC_CHAT)) == 1
    repo = monitor.repository
    assert repo.last_success(Source.MINECRAFT) == world.clock.now
    assert repo.last_success(Source.NEOFORGE) != world.clock.now

    world.bring_up(Source.NEOFORGE)
    world.add_neoforge("26.3.0.77")
    recovered = monitor.run_cycle()
    assert all(item.ok for item in recovered.sources)
    assert len(world.texts(NF_CHAT)) == 1


def test_an_unexpected_error_in_one_source_is_contained(world: World) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1")

    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in a transport")

    world.web.route(METADATA_URL, explode)

    report = monitor.run_cycle()

    by_source = {item.source: item for item in report.sources}
    assert not by_source[Source.NEOFORGE].ok
    assert by_source[Source.NEOFORGE].error == "RuntimeError"
    assert len(world.texts(MC_CHAT)) == 1


def test_telegram_outage_keeps_the_post_pending_and_recovery_delivers_it_once(
    world: World,
) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1")
    world.telegram_mode = "down"

    report = monitor.run_cycle()

    assert world.messages == []
    assert report.delivery.retried == 1
    stored = monitor.repository.get_release(Source.MINECRAFT, "26.3.1")
    assert stored is not None
    assert stored.status is ReleaseStatus.QUEUED

    world.telegram_mode = "ok"
    monitor.run_cycle()
    assert world.messages == []  # backoff has not elapsed yet

    world.clock.advance(hours=2)
    monitor.run_cycle()
    monitor.run_cycle()

    assert len(world.texts(MC_CHAT)) == 1
    stored = monitor.repository.get_release(Source.MINECRAFT, "26.3.1")
    assert stored is not None
    assert stored.status is ReleaseStatus.ANNOUNCED


# -- crash recovery ---------------------------------------------------------------------------


def crash_while_sending(world: World) -> None:
    """Leave one notification in ``sending``, as a process kill mid-request would."""
    monitor = baseline(world)
    world.add_minecraft("26.3.1")
    world.telegram_mode = "down"
    monitor.run_cycle()  # queued, first attempt fails
    world.clock.advance(days=1)  # the retry backoff has elapsed
    claimed = monitor.repository.claim_next_due(world.clock.now)
    assert claimed is not None  # 'sending': the process "dies" before knowing the outcome
    world.stop()
    world.telegram_mode = "ok"


def test_a_notification_interrupted_mid_send_is_retried_by_default(world: World) -> None:
    crash_while_sending(world)

    monitor = started(world)  # restart resolves the ambiguous row
    monitor.run_cycle()

    assert len(world.texts(MC_CHAT)) == 1  # at-least-once: a duplicate is possible, a miss is not
    notification = monitor.repository.get_notification(1)
    assert notification is not None
    assert notification.state is NotificationState.SENT


def test_a_notification_interrupted_mid_send_is_skipped_under_the_skip_policy(
    world: World,
) -> None:
    crash_while_sending(world)

    monitor = started(world, ambiguous_policy="skip")
    monitor.run_cycle()

    assert world.messages == []
    notification = monitor.repository.get_notification(1)
    assert notification is not None
    assert notification.state is NotificationState.FAILED_PERMANENT
    assert notification.last_error_class == "ambiguous_skipped"


def test_an_ambiguous_timeout_inside_a_running_process_is_queued_for_retry(world: World) -> None:
    monitor = baseline(world)
    world.add_minecraft("26.3.1")
    world.telegram_mode = "ambiguous"

    monitor.run_cycle()
    assert world.messages == []

    world.telegram_mode = "ok"
    world.clock.advance(minutes=5)
    monitor.run_cycle()
    assert len(world.texts(MC_CHAT)) == 1


# -- legacy database -----------------------------------------------------------------------------


def test_a_migrated_v1_database_does_not_repost_history(world: World) -> None:
    build_legacy_db(
        world.config.db_path,
        seeded={
            "minecraft": ["26.3", "26.2", "26.1.2", "26.1", "1.21.1"],
            "neoforge": [
                v for v in world.nf_versions if v.startswith("26.") and not v.startswith("26.1.0.0")
            ]
            + ["26.1.0.0-alpha.1+snapshot-1", "26.1.0.0-alpha.10+snapshot-6"]
            + ["26.1.0.0-alpha.11+snapshot-7", "26.1.0.0-alpha.15+pre-3"],
        },
    )

    monitor = started(world)
    monitor.run_cycle()

    assert world.messages == []
    assert Path(f"{world.config.db_path}.bak-v1").exists()

    world.add_minecraft("26.3.1")
    monitor.run_cycle()
    assert len(world.texts(MC_CHAT)) == 1


# -- the loop -------------------------------------------------------------------------------------


def test_run_forever_beats_after_each_cycle_and_stops_promptly(world: World) -> None:
    beats = threading.Event()
    stop = threading.Event()
    count = 0

    def beat() -> None:
        nonlocal count
        count += 1
        beats.set()

    world.start()
    monitor = world.monitor
    assert monitor is not None
    monitor.set_heartbeat(beat)
    worker = threading.Thread(target=monitor.run_forever, args=(stop,))
    worker.start()

    assert beats.wait(timeout=10)
    stop.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert count >= 1


def test_run_forever_survives_a_failing_cycle(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    calls = 0
    monitor = world.start()

    def flaky(stop: threading.Event | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        if calls == 3:
            assert stop is not None
            stop.set()

    monkeypatch.setattr(monitor, "run_cycle", flaky)
    worker = threading.Thread(target=monitor.run_forever, args=(stop,), kwargs={"interval": 0.01})
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert calls == 3


# -- backfill and rebaseline ----------------------------------------------------------------------


def test_backfill_dry_run_lists_candidates_and_posts_nothing(world: World) -> None:
    monitor = baseline(world)

    candidates = monitor.backfill(Source.MINECRAFT, since=None, limit=2, execute=False)

    assert [item.version for item in candidates] == ["26.2", "26.3"]  # newest 2, oldest first
    assert world.messages == []
    assert monitor.repository.get_notification(1) is None


def test_backfill_with_yes_posts_oldest_first_and_marks_origin(world: World) -> None:
    monitor = baseline(world)

    monitor.backfill(Source.MINECRAFT, since="26.1.2", limit=10, execute=True)

    texts = world.texts(MC_CHAT)
    versions = [text.split("<b>Version:</b> ")[1].split("\n")[0] for text in texts]
    assert versions == ["26.1.2", "26.2", "26.3"]
    notification = monitor.repository.get_notification(1)
    assert notification is not None
    assert notification.origin is Origin.BACKFILL


def test_backfill_respects_filters_and_never_reposts_sent_versions(world: World) -> None:
    monitor = baseline(world, neoforge_betas=False)
    world.add_neoforge("26.3.0.77")
    monitor.run_cycle()
    assert len(world.texts(NF_CHAT)) == 1

    candidates = monitor.backfill(Source.NEOFORGE, since="26.3.0.65-beta", limit=50, execute=True)

    versions = [item.version for item in candidates]
    assert "26.3.0.77" not in versions  # already announced
    assert all("beta" not in version and "alpha" not in version for version in versions)
    assert versions == ["26.3.0.76"]


def test_backfill_rejects_an_unknown_since_version(world: World) -> None:
    monitor = baseline(world)

    with pytest.raises(ValueError, match="not found"):
        monitor.backfill(Source.MINECRAFT, since="0.0.0", limit=5, execute=False)


def test_rebaseline_records_history_without_posting(world: World) -> None:
    monitor = started(world)

    count = monitor.rebaseline(Source.MINECRAFT)

    assert count > 0
    assert world.messages == []
    assert monitor.repository.is_initialized(Source.MINECRAFT)
    assert "26.3" in monitor.repository.known_versions(Source.MINECRAFT)
