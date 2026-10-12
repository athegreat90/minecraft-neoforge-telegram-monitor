"""Command line entry point: ``release-monitor [command]`` / ``python -m release_monitor``.

Exit codes: 0 success, 1 runtime failure (also "unhealthy"), 2 invalid configuration or
usage, 3 another instance already uses the database, 4 database problem.
"""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import httpx

from release_monitor import __version__
from release_monitor.app import USER_AGENT, Monitor, build_monitor
from release_monitor.config import Config, ConfigError
from release_monitor.health import check_health, write_heartbeat
from release_monitor.http import make_client
from release_monitor.logging_config import configure_logging
from release_monitor.models import Source
from release_monitor.notifier.telegram import DeliveryError, TelegramClient
from release_monitor.sources.base import SourceError
from release_monitor.storage import (
    AlreadyRunningError,
    InstanceLock,
    StorageError,
    backup,
    connect,
    open_database,
)

LOG = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3
EXIT_DATABASE = 4

DEFAULT_BACKFILL_LIMIT = 10
SOURCES = tuple(source.value for source in Source)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-monitor",
        description="Post new Minecraft Java Edition and NeoForge releases to Telegram.",
    )
    parser.add_argument("--once", action="store_true", help="legacy alias of the 'once' command")
    parser.add_argument(
        "--test", action="store_true", help="legacy alias of the 'test-telegram' command"
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser("run", help="poll forever (the default)")
    commands.add_parser("once", help="run a single poll cycle and exit")

    test = commands.add_parser(
        "test-telegram", help="check the bot token and post a labelled test message"
    )
    test.add_argument(
        "--no-send", action="store_true", help="only check the token; post no message"
    )

    commands.add_parser("healthcheck", help="exit 0 when healthy (used by Docker)")

    backfill = commands.add_parser(
        "backfill", help="announce past releases (lists them unless --yes is given)"
    )
    backfill.add_argument("--source", required=True, choices=SOURCES)
    backfill.add_argument("--since", help="oldest version to include")
    backfill.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BACKFILL_LIMIT,
        help=f"announce at most this many, newest first (default {DEFAULT_BACKFILL_LIMIT})",
    )
    backfill.add_argument("--yes", action="store_true", help="really post them")

    rebaseline = commands.add_parser(
        "rebaseline", help="record every current version as known without posting"
    )
    rebaseline.add_argument("--source", required=True, choices=SOURCES)
    rebaseline.add_argument("--yes", action="store_true", help="apply the change")

    dump = commands.add_parser("db-backup", help="write a consistent copy of the database")
    dump.add_argument("--dest", type=Path, help="target file (default: next to the database)")

    commands.add_parser("version", help="print the version")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    stop: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the CLI and return the process exit code.

    ``environ``, ``transport``, ``stop`` and ``sleep`` exist for tests: they replace the process
    environment, the network, the signal-driven shutdown and real waiting.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    env = os.environ if environ is None else environ
    command = args.command or ("test-telegram" if args.test else "once" if args.once else "run")

    if command == "version":
        print(f"release-monitor {__version__}")
        return EXIT_OK
    config = _load_config(env, quiet=command == "healthcheck")
    if isinstance(config, int):
        return config
    if command == "healthcheck":
        return _healthcheck(config)

    configure_logging(config.log_level, config.log_format, [config.token])
    if command == "test-telegram":
        return _test_telegram(config, transport, send=not getattr(args, "no_send", False))
    if command == "db-backup":
        return _db_backup(config, args.dest)
    if command == "rebaseline" and not args.yes:
        print(
            f"This records every current {args.source} version as already announced and posts "
            "nothing. Re-run with --yes to apply it."
        )
        return EXIT_OK

    def action(monitor: Monitor) -> int:
        match command:
            case "run":
                return _run(monitor, stop)
            case "once":
                return _once(monitor)
            case "backfill":
                return _backfill(monitor, args)
            case _:
                return _rebaseline(monitor, Source(args.source))

    return _with_runtime(config, transport, sleep, action)


# -- plumbing -----------------------------------------------------------------------------


def _load_config(env: Mapping[str, str], *, quiet: bool) -> Config | int:
    try:
        return Config.from_env(env)
    except ConfigError as exc:
        if quiet:  # healthcheck: one line, the reason is in the service's own log
            print("unhealthy: invalid configuration")
            return EXIT_FAILURE
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


def _with_runtime(
    config: Config,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    action: Callable[[Monitor], int],
) -> int:
    """Lock, open the database, wire the monitor, run ``action`` and clean up in reverse."""
    lock = InstanceLock(config.db_path)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except OSError as exc:
        print(f"error: cannot create the lock file: {type(exc).__name__}", file=sys.stderr)
        return EXIT_DATABASE
    try:
        try:
            conn = open_database(config.db_path)
        except (StorageError, sqlite3.Error) as exc:
            print(f"error: {_describe(exc)}", file=sys.stderr)
            return EXIT_DATABASE
        try:
            client = make_client(config.http_timeout_seconds, USER_AGENT, transport)
            try:
                monitor = build_monitor(
                    config,
                    conn,
                    client,
                    sleep=sleep,
                    heartbeat=lambda: write_heartbeat(config.db_path),
                )
                monitor.recover_on_start()
                return action(monitor)
            finally:
                client.close()
        finally:
            conn.close()
    finally:
        lock.release()


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, StorageError) else f"database error ({type(exc).__name__})"


# -- commands -----------------------------------------------------------------------------


def _run(monitor: Monitor, stop: threading.Event | None) -> int:
    event = stop if stop is not None else threading.Event()
    restore = _install_signal_handlers(event) if stop is None else None
    try:
        monitor.run_forever(event)
    finally:
        if restore is not None:
            restore()
    return EXIT_OK


def _install_signal_handlers(stop: threading.Event) -> Callable[[], None]:
    """Turn SIGTERM/SIGINT into a clean shutdown request; returns an undo function."""

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        LOG.info("received %s: finishing the current step and shutting down", _signal_name(signum))
        stop.set()

    previous: dict[int, object] = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, request_stop)
    except ValueError:  # not the main thread: the caller owns shutdown
        return lambda: None

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]

    return restore


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - only for unknown signal numbers
        return str(signum)


def _once(monitor: Monitor) -> int:
    report = monitor.run_cycle()
    return EXIT_OK if all(item.ok for item in report.sources) else EXIT_FAILURE


def _healthcheck(config: Config) -> int:
    status = check_health(config.db_path, config.poll_seconds)
    print(("healthy: " if status.ok else "unhealthy: ") + status.reason)
    return EXIT_OK if status.ok else EXIT_FAILURE


def _test_telegram(config: Config, transport: httpx.BaseTransport | None, *, send: bool) -> int:
    client = make_client(config.http_timeout_seconds, USER_AGENT, transport)
    try:
        telegram = TelegramClient(config.token, client)
        me = telegram.get_me()
        print(f"Bot token accepted (bot: @{me.get('username', 'unknown')}).")
        if not send:
            print("No message was posted (--no-send).")
            return EXIT_OK
        for label, chat in (
            ("Minecraft", config.minecraft_channel_id),
            ("NeoForge", config.neoforge_channel_id),
        ):
            telegram.send_message(
                chat,
                f"✅ <b>{label} monitor connected</b>\n"
                "This is a test message from release-monitor; no state was changed.",
            )
            print(f"Test message delivered to the {label} channel ({chat}).")
    except DeliveryError as exc:
        print(f"Telegram check failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        client.close()
    return EXIT_OK


def _backfill(monitor: Monitor, args: argparse.Namespace) -> int:
    source = Source(args.source)
    try:
        candidates = monitor.backfill(source, since=args.since, limit=args.limit, execute=args.yes)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except SourceError as exc:
        print(f"error: could not read the {source.value} releases: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if not candidates:
        print(f"Nothing to backfill for {source.value} (filters, --since and history applied).")
        return EXIT_OK
    verb = "Queued" if args.yes else "Would announce"
    print(f"{verb} {len(candidates)} {source.value} release(s), oldest first:")
    for release in candidates:
        print(f"  - {release.version} ({release.kind.value})")
    if args.yes:
        print("Delivery was attempted; the log shows any retries.")
    else:
        print("Nothing was sent. Re-run with --yes to post them.")
    return EXIT_OK


def _rebaseline(monitor: Monitor, source: Source) -> int:
    try:
        count = monitor.rebaseline(source)
    except SourceError as exc:
        print(f"error: could not read the {source.value} releases: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"Recorded {count} new {source.value} version(s) as known. Nothing was posted.")
    return EXIT_OK


def _db_backup(config: Config, dest: Path | None) -> int:
    if not config.db_path.exists():
        print(f"error: no database at {config.db_path}", file=sys.stderr)
        return EXIT_DATABASE
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest or config.db_path.with_name(f"{config.db_path.name}.{stamp}.bak")
    try:
        conn = connect(config.db_path)
        try:
            backup(conn, target)
        finally:
            conn.close()
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (StorageError, sqlite3.Error) as exc:
        print(f"error: {_describe(exc)}", file=sys.stderr)
        return EXIT_DATABASE
    print(f"Database copied to {target}")
    return EXIT_OK
