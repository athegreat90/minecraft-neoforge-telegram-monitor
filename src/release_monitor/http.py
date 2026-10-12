"""Shared HTTP client helpers: retries with backoff and token-safe errors."""

import email.utils
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

LOG = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_BOT_TOKEN_IN_PATH = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_MAX_BODY_CHARS = 2000


def redact_url(url: str | httpx.URL) -> str:
    """Return ``scheme://host/path`` with bot tokens masked and the query string dropped."""
    parsed = httpx.URL(str(url))
    path = _BOT_TOKEN_IN_PATH.sub("bot***", parsed.path)
    return f"{parsed.scheme}://{parsed.host}{path}"


class HttpError(Exception):
    """A failed request. Its text never contains the full URL or a bot token."""

    def __init__(
        self,
        description: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


def make_client(
    timeout: float,
    user_agent: str,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Create the reusable client used for every outbound request."""
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        follow_redirects=True,
        transport=transport,
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    max_retry_after: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying network errors, 408, 429 and 5xx with exponential backoff.

    ``Retry-After`` is honoured exactly (capped at ``max_retry_after``). Other 4xx
    responses are not retried. Failures raise ``HttpError`` with a redacted description.
    """
    safe_url = redact_url(url)
    for attempt in range(1, max_attempts + 1):
        retry_after: float | None = None
        failure: HttpError
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            failure = HttpError(f"{type(exc).__name__} for {method} {safe_url}")
        else:
            if response.status_code < 400:
                return response
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            failure = HttpError(
                f"HTTP {response.status_code} for {method} {safe_url}",
                status_code=response.status_code,
                body=_BOT_TOKEN_IN_PATH.sub("bot***", response.text[:_MAX_BODY_CHARS]),
                retry_after=retry_after,
            )
            if response.status_code not in RETRYABLE_STATUS:
                raise failure
        if attempt == max_attempts:
            raise failure
        delay = (
            min(retry_after, max_retry_after)
            if retry_after is not None
            else min(max_delay, base_delay * 2 ** (attempt - 1) + jitter() * base_delay)
        )
        LOG.warning(
            "request failed (%s); retry %d/%d in %.1fs",
            failure.description,
            attempt,
            max_attempts - 1,
            delay,
        )
        sleep(delay)
    raise AssertionError("unreachable: max_attempts must be >= 1")  # pragma: no cover
