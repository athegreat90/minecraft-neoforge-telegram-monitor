"""Minimal Telegram Bot API client with delivery outcomes the outbox can act on.

The bot token lives in the request URL, so nothing here ever puts a URL, a raw transport
exception or a response body into an error: messages are built from status codes, Telegram's
own ``description`` (scrubbed) and exception type names only.
"""

import logging
import re
from typing import Any

import httpx

from release_monitor.http import parse_retry_after

LOG = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
DEFAULT_RETRY_AFTER = 30.0
_BOT_TOKEN_IN_TEXT = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_MAX_DESCRIPTION_CHARS = 300

# Failures where the request provably never reached Telegram.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class DeliveryError(Exception):
    """Base class for every outcome of a failed send."""


class RateLimited(DeliveryError):
    """HTTP 429: retry after ``retry_after`` seconds."""

    def __init__(self, retry_after: float, description: str = "") -> None:
        super().__init__(f"Telegram rate limit: retry after {retry_after:g}s {description}".strip())
        self.retry_after = retry_after


class PermanentDeliveryError(DeliveryError):
    """Retrying cannot help (bad chat id, missing permissions, bad token, bad formatting)."""


class TransientDeliveryError(DeliveryError):
    """The message was not delivered and a later retry may succeed."""


class AmbiguousDeliveryError(DeliveryError):
    """The request may or may not have been delivered; a blind retry can duplicate it."""


class TelegramClient:
    """Sends HTML messages to channels and checks the bot's identity."""

    def __init__(self, token: str, client: httpx.Client) -> None:
        if not token.strip():
            raise ValueError("a Telegram bot token is required")
        self._token = token.strip()
        self._client = client
        _quiet_http_loggers()

    def __repr__(self) -> str:
        return "TelegramClient(token=***)"

    def get_me(self) -> dict[str, Any]:
        """Return the bot's own description; raises on an invalid token or network problem."""
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    def send_message(self, chat_id: str, text: str) -> int:
        """Send ``text`` (Telegram HTML) to ``chat_id`` and return its message id."""
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise AmbiguousDeliveryError("Telegram accepted the request but returned no message id")
        return message_id

    # -- internals ----------------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        try:
            response = self._client.post(url, json=payload)
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from None
        return self._handle(response, method)

    def _handle(self, response: httpx.Response, method: str) -> Any:
        status = response.status_code
        body = _json_object(response)
        description = self._scrub(str(body.get("description", ""))) if body else ""
        if status == httpx.codes.OK:
            if body is None:
                raise AmbiguousDeliveryError(
                    f"Telegram returned an unreadable success response to {method}"
                )
            if body.get("ok") is True:
                return body.get("result")
            raise PermanentDeliveryError(self._rejected(status, description))
        if status == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimited(_retry_after(body, response), description)
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise TransientDeliveryError(f"Telegram server error (HTTP {status}) on {method}")
        raise PermanentDeliveryError(self._rejected(status, description))

    def _rejected(self, status: int, description: str) -> str:
        detail = f": {description}" if description else ""
        hint = _hint(status, description)
        return f"Telegram rejected the request (HTTP {status}){detail}. {hint}"

    def _scrub(self, text: str) -> str:
        cleaned = _BOT_TOKEN_IN_TEXT.sub("bot***", text.replace(self._token, "***"))
        return cleaned[:_MAX_DESCRIPTION_CHARS]

    @staticmethod
    def _transport_error(exc: httpx.TransportError) -> DeliveryError:
        name = type(exc).__name__
        if isinstance(exc, _NOT_SENT):
            return TransientDeliveryError(f"could not reach Telegram ({name})")
        return AmbiguousDeliveryError(
            f"no confirmation from Telegram ({name}); "
            "the message may or may not have been delivered"
        )


def _quiet_http_loggers() -> None:
    """Stop httpx logging request URLs: at INFO it prints ``POST .../bot<TOKEN>/sendMessage``.

    ``configure_logging`` does the same for the application; repeating it here keeps the
    client safe when it is used on its own (tests, scripts, a forgotten logging setup).
    """
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _retry_after(body: dict[str, Any] | None, response: httpx.Response) -> float:
    parameters = body.get("parameters") if body else None
    if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int | float):
        return float(parameters["retry_after"])
    header = parse_retry_after(response.headers.get("Retry-After"))
    return header if header is not None else DEFAULT_RETRY_AFTER


def _hint(status: int, description: str) -> str:
    lowered = description.lower()
    if "chat not found" in lowered:
        return (
            "Check the chat id (public channels use @name, private ones a numeric -100... id) "
            "and that the bot is an administrator of the channel."
        )
    if "parse entities" in lowered or "can't parse" in lowered:
        return "The message formatting was rejected; this is a bug in the monitor."
    if "too long" in lowered:
        return "The message exceeded Telegram's length limit; this is a bug in the monitor."
    if status == httpx.codes.UNAUTHORIZED:
        return "Check TELEGRAM_BOT_TOKEN: Telegram does not accept this token."
    if status == httpx.codes.FORBIDDEN:
        return "Make the bot an administrator of the channel with permission to post messages."
    if status == httpx.codes.NOT_FOUND:
        return "Check TELEGRAM_BOT_TOKEN: Telegram does not recognise this bot."
    return "Check the bot token, the chat id and the bot's channel permissions."
