import httpx
import pytest

from release_monitor.http import HttpError, make_client, request_with_retry

TOKEN = "123456:AAFAKE-token_value"
TOKEN_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def client_for(responses: list[httpx.Response | Exception]) -> tuple[httpx.Client, list[int]]:
    calls: list[int] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return make_client(5.0, "test-agent", transport=httpx.MockTransport(handler)), calls


def test_success_first_try() -> None:
    client, calls = client_for([httpx.Response(200, text="ok")])
    sleeper = Sleeper()
    with client:
        response = request_with_retry(client, "GET", "https://example.org/x", sleep=sleeper)
    assert response.text == "ok"
    assert len(calls) == 1
    assert sleeper.calls == []


def test_user_agent_header_is_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200)

    with make_client(5.0, "my-agent/1", transport=httpx.MockTransport(handler)) as client:
        request_with_retry(client, "GET", "https://example.org/x", sleep=Sleeper())
    assert seen == ["my-agent/1"]


def test_retry_on_503_then_success() -> None:
    client, calls = client_for([httpx.Response(503), httpx.Response(503), httpx.Response(200)])
    sleeper = Sleeper()
    with client:
        response = request_with_retry(
            client, "GET", "https://example.org/x", sleep=sleeper, jitter=lambda: 0.0
        )
    assert response.status_code == 200
    assert len(calls) == 3
    assert len(sleeper.calls) == 2
    assert sleeper.calls[1] > sleeper.calls[0]  # exponential


def test_429_honours_retry_after() -> None:
    client, _ = client_for([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    sleeper = Sleeper()
    with client:
        request_with_retry(client, "POST", TOKEN_URL, sleep=sleeper, jitter=lambda: 0.0)
    assert sleeper.calls == [7.0]


def test_retry_after_is_capped() -> None:
    client, _ = client_for(
        [httpx.Response(429, headers={"Retry-After": "99999"}), httpx.Response(200)]
    )
    sleeper = Sleeper()
    with client:
        request_with_retry(
            client, "GET", "https://example.org/x", sleep=sleeper, max_retry_after=60.0
        )
    assert sleeper.calls == [60.0]


def test_no_retry_on_404() -> None:
    client, calls = client_for([httpx.Response(404)])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(client, "GET", "https://example.org/x", sleep=Sleeper())
    assert info.value.status_code == 404
    assert len(calls) == 1


def test_no_retry_on_400_returns_status_for_caller() -> None:
    client, calls = client_for([httpx.Response(400, json={"ok": False})])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(client, "POST", TOKEN_URL, sleep=Sleeper())
    assert info.value.status_code == 400
    assert len(calls) == 1


def test_exhaustion_raises_http_error() -> None:
    client, calls = client_for([httpx.Response(500)] * 3)
    with client, pytest.raises(HttpError) as info:
        request_with_retry(
            client,
            "GET",
            "https://example.org/x",
            max_attempts=3,
            sleep=Sleeper(),
            jitter=lambda: 0.0,
        )
    assert info.value.status_code == 500
    assert len(calls) == 3


def test_network_errors_are_retried_and_wrapped() -> None:
    client, calls = client_for([httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(
            client,
            "GET",
            "https://example.org/x",
            max_attempts=2,
            sleep=Sleeper(),
            jitter=lambda: 0.0,
        )
    assert info.value.status_code is None
    assert len(calls) == 2


def test_network_error_then_success() -> None:
    client, _ = client_for([httpx.ConnectError("boom"), httpx.Response(200)])
    with client:
        response = request_with_retry(
            client, "GET", "https://example.org/x", sleep=Sleeper(), jitter=lambda: 0.0
        )
    assert response.status_code == 200


def test_error_text_never_leaks_token_url() -> None:
    client, _ = client_for([httpx.ConnectError(f"cannot reach {TOKEN_URL}")])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(client, "POST", TOKEN_URL, max_attempts=1, sleep=Sleeper())
    text = f"{info.value} {info.value!r} {info.value.description}"
    assert TOKEN not in text
    assert "AAFAKE" not in text
    assert info.value.__cause__ is None or TOKEN not in str(info.value.__cause__)


def test_status_error_text_never_leaks_token_url() -> None:
    client, _ = client_for([httpx.Response(401)])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(client, "POST", TOKEN_URL, sleep=Sleeper())
    assert TOKEN not in str(info.value)
    assert "AAFAKE" not in str(info.value)
    assert "401" in str(info.value)


def test_response_body_is_available_on_error() -> None:
    client, _ = client_for([httpx.Response(400, json={"ok": False, "description": "bad"})])
    with client, pytest.raises(HttpError) as info:
        request_with_retry(client, "POST", TOKEN_URL, sleep=Sleeper())
    assert info.value.body is not None
    assert "bad" in info.value.body
