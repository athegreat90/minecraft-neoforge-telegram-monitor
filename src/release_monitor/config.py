"""Environment-based configuration with fail-fast validation."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_POLL_SECONDS = 60
_TRUE = frozenset({"true", "yes", "1", "on"})
_FALSE = frozenset({"false", "no", "0", "off"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_PUBLIC_CHANNEL = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
_NUMERIC_CHAT = re.compile(r"^-?\d+$")

REDACTED = "***REDACTED***"


class ConfigError(ValueError):
    """Raised with every configuration problem found, never with secret values."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration. The bot token is hidden from ``repr``."""

    token: str = field(repr=False)
    minecraft_channel_id: str
    neoforge_channel_id: str
    minecraft_snapshots: bool = False
    minecraft_prereleases: bool = False
    neoforge_betas: bool = True
    neoforge_min_major: int = 26
    poll_seconds: int = 300
    db_path: Path = Path("/data/releases.sqlite3")
    changelog_grace_minutes: int = 30
    ambiguous_policy: str = "retry"
    display_timezone: str = "UTC"
    log_level: str = "INFO"
    log_format: str = "text"
    http_timeout_seconds: float = 25.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Config:
        """Build a ``Config`` from ``environ`` or raise one aggregated ``ConfigError``."""
        parser = _Parser(environ)
        token = parser.token()
        minecraft_channel = parser.channel("MINECRAFT_CHANNEL_ID")
        neoforge_channel = parser.channel("NEOFORGE_CHANNEL_ID")
        config = cls(
            token=token,
            minecraft_channel_id=minecraft_channel,
            neoforge_channel_id=neoforge_channel,
            minecraft_snapshots=parser.boolean("MINECRAFT_SNAPSHOTS", False),
            minecraft_prereleases=parser.boolean("MINECRAFT_PRERELEASES", False),
            neoforge_betas=parser.boolean("NEOFORGE_BETAS", True),
            neoforge_min_major=parser.integer("NEOFORGE_MIN_MAJOR", 26, minimum=0),
            poll_seconds=parser.integer("POLL_SECONDS", 300, minimum=MIN_POLL_SECONDS),
            db_path=Path(parser.text("DB_PATH", "/data/releases.sqlite3")),
            changelog_grace_minutes=parser.integer("CHANGELOG_GRACE_MINUTES", 30, minimum=0),
            ambiguous_policy=parser.choice("AMBIGUOUS_POLICY", "retry", {"retry", "skip"}),
            display_timezone=parser.timezone("DISPLAY_TIMEZONE", "UTC"),
            log_level=parser.choice("LOG_LEVEL", "INFO", _LOG_LEVELS, upper=True),
            log_format=parser.choice("LOG_FORMAT", "text", {"text", "json"}),
            http_timeout_seconds=parser.number("HTTP_TIMEOUT_SECONDS", 25.0),
        )
        parser.raise_if_errors()
        return config

    def redacted(self) -> dict[str, object]:
        """Return a log-safe mapping of the settings with the token masked."""
        return {
            "token": REDACTED,
            "minecraft_channel_id": self.minecraft_channel_id,
            "neoforge_channel_id": self.neoforge_channel_id,
            "minecraft_snapshots": self.minecraft_snapshots,
            "minecraft_prereleases": self.minecraft_prereleases,
            "neoforge_betas": self.neoforge_betas,
            "neoforge_min_major": self.neoforge_min_major,
            "poll_seconds": self.poll_seconds,
            "db_path": str(self.db_path),
            "changelog_grace_minutes": self.changelog_grace_minutes,
            "ambiguous_policy": self.ambiguous_policy,
            "display_timezone": self.display_timezone,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "http_timeout_seconds": self.http_timeout_seconds,
        }


class _Parser:
    """Collects problems instead of failing on the first one."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._env = environ
        self._errors: list[str] = []

    def raise_if_errors(self) -> None:
        if self._errors:
            joined = "\n  - ".join(self._errors)
            raise ConfigError(f"Invalid configuration:\n  - {joined}")

    def _raw(self, name: str) -> str:
        return self._env.get(name, "").strip()

    def _fail(self, message: str) -> None:
        self._errors.append(message)

    def text(self, name: str, default: str) -> str:
        return self._raw(name) or default

    def token(self) -> str:
        direct = self._raw("TELEGRAM_BOT_TOKEN")
        if direct:
            return direct
        file_name = self._raw("TELEGRAM_BOT_TOKEN_FILE")
        if not file_name:
            self._fail("TELEGRAM_BOT_TOKEN is required (or set TELEGRAM_BOT_TOKEN_FILE)")
            return ""
        try:
            content = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            self._fail(f"TELEGRAM_BOT_TOKEN_FILE is not readable: {type(exc).__name__}")
            return ""
        if not content:
            self._fail("TELEGRAM_BOT_TOKEN_FILE points to an empty file")
        return content

    def channel(self, name: str) -> str:
        value = self._raw(name)
        if not value:
            self._fail(f"{name} is required (@public_channel or numeric chat id)")
        elif not (_PUBLIC_CHANNEL.match(value) or _NUMERIC_CHAT.match(value)):
            self._fail(
                f"{name} must be '@name' (5-32 letters, digits or underscores) "
                "or a numeric chat id such as -1001234567890"
            )
        return value

    def boolean(self, name: str, default: bool) -> bool:
        value = self._raw(name).lower()
        if not value:
            return default
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        self._fail(f"{name} must be one of true/yes/1/on or false/no/0/off")
        return default

    def integer(self, name: str, default: int, *, minimum: int) -> int:
        value = self._raw(name)
        if not value:
            return default
        try:
            number = int(value)
        except ValueError:
            self._fail(f"{name} must be an integer")
            return default
        if number < minimum:
            self._fail(f"{name} must be >= {minimum}")
            return default
        return number

    def number(self, name: str, default: float) -> float:
        value = self._raw(name)
        if not value:
            return default
        try:
            number = float(value)
        except ValueError:
            self._fail(f"{name} must be a number")
            return default
        if not number > 0:
            self._fail(f"{name} must be greater than 0")
            return default
        return number

    def choice(
        self, name: str, default: str, allowed: set[str] | frozenset[str], *, upper: bool = False
    ) -> str:
        value = self._raw(name)
        if not value:
            return default
        value = value.upper() if upper else value.lower()
        if value not in allowed:
            self._fail(f"{name} must be one of: {', '.join(sorted(allowed))}")
            return default
        return value

    def timezone(self, name: str, default: str) -> str:
        value = self._raw(name) or default
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            self._fail(f"{name} is not a known IANA time zone")
            return default
        return value
