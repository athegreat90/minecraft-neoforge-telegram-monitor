"""Helpers for tests that read captured fixtures or fake the network."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from release_monitor.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_TOKEN = "123456:AAFAKE-token_value"

Route = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def make_config(**overrides: Any) -> Config:
    base = Config(
        token=FAKE_TOKEN,
        minecraft_channel_id="@mc_test_channel",
        neoforge_channel_id="@nf_test_channel",
    )
    return replace(base, **overrides)


class FakeWeb:
    """Routes ``(method, url)`` to canned responses; unknown requests get a 404.

    A route may be a response, an exception to raise, or a callable. Method ``"*"`` matches any
    method. Every request is recorded in ``requests``.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Route] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self,
        url: str,
        *,
        method: str = "GET",
        status: int = 200,
        text: str | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        if error is not None:
            self._routes[(method, url)] = error
            return
        body = content if content is not None else (text or "").encode("utf-8")
        self._routes[(method, url)] = httpx.Response(status, content=body, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        route = self._routes.get((request.method, url)) or self._routes.get(("*", url))
        if route is None:
            return httpx.Response(404, content=b"not found")
        if isinstance(route, Exception):
            raise route
        if isinstance(route, httpx.Response):
            return httpx.Response(route.status_code, content=route.content, headers=route.headers)
        return route(request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def count(self, url: str, method: str | None = None) -> int:
        return sum(
            1
            for request in self.requests
            if str(request.url) == url and (method is None or request.method == method)
        )


def no_sleep(_seconds: float) -> None:
    """Replaces ``time.sleep`` so retry tests run instantly."""
