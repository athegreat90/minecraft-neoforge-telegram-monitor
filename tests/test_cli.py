"""The command line: exit codes, safe output, shutdown and the operator commands."""

import logging
import runpy
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

from release_monitor import __version__
from release_monitor.app import Monitor
from release_monitor.cli import _install_signal_handlers, _run, main
from release_monitor.health import heartbeat_path, write_heartbeat
from release_monitor.models import Source
from release_monitor.sources.minecraft import MANIFEST_URL
from release_monitor.sources.neoforge import METADATA_URL
from release_monitor.storage import InstanceLock
from tests.fixtures_util import FAKE_TOKEN, no_sleep
from tests.world import MC_CHAT, NF_CHAT, World


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """``main`` reconfigures the root logger; put it back so other tests are unaffected."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    instance = World(tmp_path)
    yield instance
    instance.stop()


def env(world: World, **extra: str) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "MINECRAFT_CHANNEL_ID": MC_CHAT,
        "NEOFORGE_CHANNEL_ID": NF_CHAT,
        "DB_PATH": str(world.config.db_path),
        **extra,
    }


def cli(world: World, *argv: str, **extra: str) -> int:
    return main(
        list(argv),
        environ=env(world, **extra),
        transport=httpx.MockTransport(world.web),
        sleep=no_sleep,
    )


def out_err(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


def assert_no_token(capsys: pytest.CaptureFixture[str]) -> None:
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err
    assert FAKE_TOKEN.split(":")[1] not in captured.out + captured.err


# -- basics ------------------------------------------------------------------------------


def test_version(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0

    assert out_err(capsys)[0].strip() == f"release-monitor {__version__}"


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["release_monitor", "version"])

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("release_monitor", run_name="__main__")

    assert raised.value.code == 0
    assert out_err(capsys)[0].startswith("release-monitor ")


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "backfill" in out_err(capsys)[0]


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == 2


def test_invalid_configuration_exits_2_without_leaking_secrets(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli(world, "once", MINECRAFT_CHANNEL_ID="not a channel", POLL_SECONDS="5")

    assert code == 2
    _, err = out_err(capsys)
    assert "MINECRAFT_CHANNEL_ID must be" in err
    assert "POLL_SECONDS must be >= 60" in err
    assert FAKE_TOKEN not in err
    assert "Traceback" not in err


def test_missing_token_is_reported_plainly(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["once"], environ={"MINECRAFT_CHANNEL_ID": MC_CHAT}, sleep=no_sleep)

    assert code == 2
    assert "TELEGRAM_BOT_TOKEN is required" in out_err(capsys)[1]


# -- once / legacy flags --------------------------------------------------------------------


def test_once_baselines_then_posts_only_new_releases(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "once") == 0
    assert world.messages == []

    world.add_minecraft("26.3.1")
    assert cli(world, "once") == 0

    assert len(world.texts(MC_CHAT)) == 1
    assert_no_token(capsys)


def test_once_exits_1_when_a_source_failed(world: World) -> None:
    world.take_down(Source.NEOFORGE)

    assert cli(world, "once") == 1


def test_legacy_once_flag_is_an_alias(world: World) -> None:
    assert cli(world, "--once") == 0

    assert world.web.count(MANIFEST_URL) == 1
    assert world.web.count(METADATA_URL) == 1


def test_legacy_test_flag_is_an_alias_of_test_telegram(world: World) -> None:
    assert cli(world, "--test") == 0

    assert len(world.texts(MC_CHAT)) == 1
    assert len(world.texts(NF_CHAT)) == 1


# -- test-telegram ---------------------------------------------------------------------------


def test_test_telegram_no_send_only_checks_the_token(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "test-telegram", "--no-send") == 0

    out, _ = out_err(capsys)
    assert "@test_bot" in out
    assert world.messages == []
    assert not world.config.db_path.exists()  # no state is touched


def test_test_telegram_posts_a_labelled_message_to_each_channel(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "test-telegram") == 0

    assert "Minecraft monitor connected" in world.texts(MC_CHAT)[0]
    assert "NeoForge monitor connected" in world.texts(NF_CHAT)[0]
    assert "no state was changed" in world.texts(MC_CHAT)[0]
    assert not world.config.db_path.exists()
    assert_no_token(capsys)


def test_test_telegram_failure_exits_1_with_a_hint_and_no_token(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.telegram_mode = "forbidden"

    assert cli(world, "test-telegram") == 1

    _, err = out_err(capsys)
    assert "administrator" in err
    assert FAKE_TOKEN not in err
    assert FAKE_TOKEN.split(":")[1] not in err


# -- startup failures ------------------------------------------------------------------------


def test_a_second_instance_exits_3(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    with InstanceLock(world.config.db_path):
        code = cli(world, "once")

    assert code == 3
    assert "another instance" in out_err(capsys)[1]
    assert world.web.requests == []  # refused before doing any work


def test_a_corrupt_database_exits_4_and_is_never_deleted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.config.db_path
    path.parent.mkdir(parents=True)
    garbage = b"definitely not a sqlite database" * 100
    path.write_bytes(garbage)

    code = cli(world, "once")

    assert code == 4
    assert "damaged" in out_err(capsys)[1]
    assert path.read_bytes() == garbage
    assert world.web.requests == []


def test_a_database_from_a_newer_version_exits_4(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "once") == 0
    conn = sqlite3.connect(world.config.db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    capsys.readouterr()

    assert cli(world, "once") == 4
    assert "error:" in out_err(capsys)[1]


# -- run / shutdown ----------------------------------------------------------------------------


def test_run_stops_cleanly_and_releases_the_lock(world: World) -> None:
    stop = threading.Event()
    stop.set()

    code = main(
        ["run"],
        environ=env(world),
        transport=httpx.MockTransport(world.web),
        stop=stop,
        sleep=no_sleep,
    )

    assert code == 0
    assert heartbeat_path(world.config.db_path).exists()
    with InstanceLock(world.config.db_path):  # would raise if the lock leaked
        pass


def test_run_polls_then_stops_promptly_when_asked(world: World) -> None:
    stop = threading.Event()
    result: list[int] = []

    def target() -> None:
        result.append(
            main(
                ["run"],
                environ=env(world, POLL_SECONDS="300"),
                transport=httpx.MockTransport(world.web),
                stop=stop,
                sleep=no_sleep,
            )
        )

    worker = threading.Thread(target=target)
    worker.start()
    deadline = time.monotonic() + 10
    while world.web.count(METADATA_URL) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    stop.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 5  # not the 300 s poll interval
    assert result == [0]
    assert world.web.count(METADATA_URL) == 1


def test_signal_handlers_request_a_stop_and_are_restored() -> None:
    stop = threading.Event()
    before = signal.getsignal(signal.SIGTERM)

    restore = _install_signal_handlers(stop)
    signal.raise_signal(signal.SIGTERM)
    assert stop.is_set()
    restore()

    assert signal.getsignal(signal.SIGTERM) == before


def test_run_installs_handlers_around_the_loop_and_undoes_them() -> None:
    class Fake:
        stopped_by_signal = False

        def run_forever(self, stop: threading.Event) -> None:
            signal.raise_signal(signal.SIGTERM)
            self.stopped_by_signal = stop.is_set()

    fake = Fake()
    before = signal.getsignal(signal.SIGTERM)

    assert _run(cast(Monitor, fake), None) == 0

    assert fake.stopped_by_signal
    assert signal.getsignal(signal.SIGTERM) == before


# -- healthcheck --------------------------------------------------------------------------------


def test_healthcheck_is_unhealthy_before_the_first_poll(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "healthcheck") == 1

    assert out_err(capsys)[0].startswith("unhealthy:")


def test_healthcheck_is_healthy_after_a_successful_poll(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "once") == 0
    write_heartbeat(world.config.db_path)
    capsys.readouterr()

    assert cli(world, "healthcheck") == 0

    out, _ = out_err(capsys)
    assert out.startswith("healthy:")
    assert world.web.requests  # polling happened in `once`...
    count = len(world.web.requests)
    assert cli(world, "healthcheck") == 0
    assert len(world.web.requests) == count  # ...but the health check makes no requests


def test_healthcheck_with_invalid_configuration_is_unhealthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["healthcheck"], environ={}) == 1

    assert "unhealthy" in out_err(capsys)[0]


# -- backfill / rebaseline / db-backup -------------------------------------------------------------


def test_backfill_without_yes_lists_and_posts_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "once") == 0
    capsys.readouterr()

    code = cli(world, "backfill", "--source", "minecraft", "--limit", "2")

    assert code == 0
    out, _ = out_err(capsys)
    assert "Would announce 2 minecraft release(s)" in out
    assert "- 26.2 (stable)" in out
    assert "- 26.3 (stable)" in out
    assert "Nothing was sent" in out
    assert world.messages == []


def test_backfill_with_yes_posts_them(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(world, "once") == 0
    capsys.readouterr()

    code = cli(world, "backfill", "--source", "minecraft", "--limit", "2", "--yes")

    assert code == 0
    assert "Queued 2 minecraft release(s)" in out_err(capsys)[0]
    assert len(world.texts(MC_CHAT)) == 2
    assert cli(world, "backfill", "--source", "minecraft", "--limit", "2", "--yes") == 0
    assert len(world.texts(MC_CHAT)) == 2  # already sent versions are never posted again


def test_backfill_with_an_unknown_version_exits_2(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "once") == 0
    capsys.readouterr()

    assert cli(world, "backfill", "--source", "minecraft", "--since", "0.0.0") == 2
    assert "not found" in out_err(capsys)[1]


def test_backfill_requires_a_source(world: World) -> None:
    assert cli(world, "backfill") == 2


def test_rebaseline_without_yes_changes_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "rebaseline", "--source", "neoforge") == 0

    assert "Re-run with --yes" in out_err(capsys)[0]
    assert not world.config.db_path.exists()
    assert world.web.requests == []


def test_rebaseline_with_yes_records_history_without_posting(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "rebaseline", "--source", "minecraft", "--yes") == 0

    assert "Nothing was posted" in out_err(capsys)[0]
    assert world.messages == []
    conn = sqlite3.connect(world.config.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    finally:
        conn.close()
    assert count > 0


def test_db_backup_copies_the_database(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(world, "once") == 0
    target = world.directory / "copy.sqlite3"

    assert cli(world, "db-backup", "--dest", str(target)) == 0

    assert target.exists()
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0] > 0
    finally:
        conn.close()
    capsys.readouterr()
    assert cli(world, "db-backup", "--dest", str(target)) == 1  # never overwrites
    assert "already exists" in out_err(capsys)[1]


def test_db_backup_defaults_to_a_file_next_to_the_database(world: World) -> None:
    assert cli(world, "once") == 0

    assert cli(world, "db-backup") == 0

    backups = list(world.config.db_path.parent.glob("releases.sqlite3.*.bak"))
    assert len(backups) == 1


def test_db_backup_without_a_database_exits_4(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(world, "db-backup") == 4
    assert "no database" in out_err(capsys)[1]


def test_db_backup_works_while_the_service_holds_the_lock(world: World) -> None:
    assert cli(world, "once") == 0

    with InstanceLock(world.config.db_path):
        assert cli(world, "db-backup", "--dest", str(world.directory / "live.sqlite3")) == 0
