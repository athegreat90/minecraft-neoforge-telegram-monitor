"""Shared types and the thin fetch layer used by every release source."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from release_monitor.http import HttpError, request_with_retry
from release_monitor.models import PublishedAtKind

DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class SourceError(Exception):
    """An upstream source could not be read or returned something unusable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReleaseDetails:
    """Official notes for one release, as far as they are available right now.

    ``ready`` is ``False`` while the upstream notes or changelog are missing or delayed;
    nothing is ever invented to fill the gap, and ``reason`` says what is missing.
    """

    ready: bool
    highlights: tuple[str, ...] = ()
    changelog_url: str | None = None
    notes_url: str | None = None
    link_label: str = "Official link"
    published_at: datetime | None = None
    published_at_kind: PublishedAtKind = PublishedAtKind.DETECTED
    reason: str | None = None

    @property
    def official_url(self) -> str | None:
        """The one link to show in a message."""
        return self.notes_url or self.changelog_url


class Fetcher:
    """GET/HEAD with retries, a response size cap and ``SourceError`` as the only failure."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_bytes = max_bytes

    def request(self, method: str, url: str, *, max_bytes: int | None = None) -> httpx.Response:
        try:
            response = request_with_retry(
                self.client, method, url, max_attempts=self._max_attempts, sleep=self._sleep
            )
        except HttpError as exc:
            raise SourceError(exc.description, status_code=exc.status_code) from None
        if len(response.content) > (max_bytes or self._max_bytes):
            raise SourceError(f"response too large for {method} {url}")
        return response

    def get(self, url: str, *, max_bytes: int | None = None) -> httpx.Response:
        return self.request("GET", url, max_bytes=max_bytes)

    def head(self, url: str) -> httpx.Response:
        return self.request("HEAD", url)
