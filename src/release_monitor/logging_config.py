"""UTC logging with secret redaction (text or JSON)."""

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import IO, Any

MASK = "***"
_BOT_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_STANDARD_ATTRS = frozenset(
    {*logging.LogRecord("", 0, "", 0, "", None, None).__dict__, "message", "asctime", "taskName"}
)
_NOISY_LOGGERS = ("httpx", "httpcore")


class RedactionFilter(logging.Filter):
    """Masks known secrets and anything shaped like a Telegram bot token URL."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        cleaned = {secret.strip() for secret in secrets if secret and secret.strip()}
        self._secrets = sorted(cleaned, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return _BOT_TOKEN_IN_URL.sub(f"bot{MASK}", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                setattr(record, key, self.redact(value))
        return True


def _utc_timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TextFormatter(logging.Formatter):
    """``2026-10-12T10:00:00.000Z INFO logger message``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return _utc_timestamp(record.created)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``extra`` keys become top-level fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str,
    fmt: str,
    secrets: Iterable[str],
    stream: IO[str] | None = None,
) -> None:
    """Replace root handlers with one redacting handler.

    ``httpx`` logs full request URLs at INFO and Telegram puts the bot token in the
    URL path, so those loggers are pinned to WARNING regardless of ``level``.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactionFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
