from pathlib import Path

import pytest

from release_monitor.config import Config, ConfigError

TOKEN = "123456:AAFAKE-token_value"


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "MINECRAFT_CHANNEL_ID": "@McReleases",
        "NEOFORGE_CHANNEL_ID": "-1001234567890",
    }
    env.update(overrides)
    return env


def test_defaults() -> None:
    cfg = Config.from_env(base_env())
    assert cfg.minecraft_channel_id == "@McReleases"
    assert cfg.neoforge_channel_id == "-1001234567890"
    assert cfg.minecraft_snapshots is False
    assert cfg.minecraft_prereleases is False
    assert cfg.neoforge_betas is True
    assert cfg.neoforge_min_major == 26
    assert cfg.poll_seconds == 300
    assert str(cfg.db_path).replace("\\", "/").endswith("data/releases.sqlite3")
    assert cfg.changelog_grace_minutes == 30
    assert cfg.ambiguous_policy == "retry"
    assert cfg.display_timezone == "UTC"
    assert cfg.log_level == "INFO"
    assert cfg.log_format == "text"
    assert cfg.http_timeout_seconds == 25.0


def test_overrides() -> None:
    cfg = Config.from_env(
        base_env(
            MINECRAFT_SNAPSHOTS="YES",
            MINECRAFT_PRERELEASES="on",
            NEOFORGE_BETAS="0",
            NEOFORGE_MIN_MAJOR="27",
            POLL_SECONDS="600",
            DB_PATH="/tmp/x.sqlite3",
            CHANGELOG_GRACE_MINUTES="0",
            AMBIGUOUS_POLICY="SKIP",
            DISPLAY_TIMEZONE="Europe/Madrid",
            LOG_LEVEL="debug",
            LOG_FORMAT="json",
            HTTP_TIMEOUT_SECONDS="10.5",
        )
    )
    assert cfg.minecraft_snapshots is True
    assert cfg.minecraft_prereleases is True
    assert cfg.neoforge_betas is False
    assert cfg.neoforge_min_major == 27
    assert cfg.poll_seconds == 600
    assert cfg.changelog_grace_minutes == 0
    assert cfg.ambiguous_policy == "skip"
    assert cfg.display_timezone == "Europe/Madrid"
    assert cfg.log_level == "DEBUG"
    assert cfg.log_format == "json"
    assert cfg.http_timeout_seconds == 10.5


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1", "on"])
def test_bool_true_values(value: str) -> None:
    assert Config.from_env(base_env(MINECRAFT_SNAPSHOTS=value)).minecraft_snapshots is True


@pytest.mark.parametrize("value", ["false", "No", "0", "OFF"])
def test_bool_false_values(value: str) -> None:
    assert Config.from_env(base_env(NEOFORGE_BETAS=value)).neoforge_betas is False


def test_poll_seconds_has_minimum() -> None:
    with pytest.raises(ConfigError, match="POLL_SECONDS"):
        Config.from_env(base_env(POLL_SECONDS="30"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MINECRAFT_SNAPSHOTS", "maybe"),
        ("NEOFORGE_MIN_MAJOR", "abc"),
        ("NEOFORGE_MIN_MAJOR", "-1"),
        ("POLL_SECONDS", "x"),
        ("CHANGELOG_GRACE_MINUTES", "-5"),
        ("AMBIGUOUS_POLICY", "maybe"),
        ("DISPLAY_TIMEZONE", "Mars/Olympus"),
        ("LOG_FORMAT", "xml"),
        ("LOG_LEVEL", "LOUD"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
        ("MINECRAFT_CHANNEL_ID", "McReleases"),
        ("MINECRAFT_CHANNEL_ID", "@abc"),
        ("NEOFORGE_CHANNEL_ID", "-100abc"),
    ],
)
def test_invalid_values_name_the_variable(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Config.from_env(base_env(**{name: value}))


def test_errors_are_aggregated() -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_env({"POLL_SECONDS": "x", "NEOFORGE_BETAS": "maybe"})
    message = str(info.value)
    for expected in (
        "TELEGRAM_BOT_TOKEN",
        "MINECRAFT_CHANNEL_ID",
        "NEOFORGE_CHANNEL_ID",
        "POLL_SECONDS",
        "NEOFORGE_BETAS",
    ):
        assert expected in message


def test_token_file(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text(f"  {TOKEN}\n", encoding="utf-8")
    env = base_env(TELEGRAM_BOT_TOKEN_FILE=str(secret))
    del env["TELEGRAM_BOT_TOKEN"]
    assert Config.from_env(env).token == TOKEN


def test_plain_token_wins_over_file(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("999:OTHER", encoding="utf-8")
    cfg = Config.from_env(base_env(TELEGRAM_BOT_TOKEN_FILE=str(secret)))
    assert cfg.token == TOKEN


def test_unreadable_token_file(tmp_path: Path) -> None:
    env = base_env(TELEGRAM_BOT_TOKEN_FILE=str(tmp_path / "missing"))
    del env["TELEGRAM_BOT_TOKEN"]
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN_FILE"):
        Config.from_env(env)


def test_empty_token_file(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("  \n", encoding="utf-8")
    env = base_env(TELEGRAM_BOT_TOKEN_FILE=str(secret))
    del env["TELEGRAM_BOT_TOKEN"]
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env(env)


def test_repr_and_str_never_reveal_the_token() -> None:
    cfg = Config.from_env(base_env())
    assert TOKEN not in repr(cfg)
    assert TOKEN not in str(cfg)
    assert TOKEN not in str(cfg.redacted())
    assert "REDACTED" in str(cfg.redacted()).upper() or "***" in str(cfg.redacted())


def test_config_error_never_contains_token_value() -> None:
    with pytest.raises(ConfigError) as info:
        Config.from_env(base_env(POLL_SECONDS="x"))
    assert TOKEN not in str(info.value)
