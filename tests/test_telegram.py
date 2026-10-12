"""Telegram client: success, error classification and token safety."""

import json
import logging
import traceback
from collections.abc import Callable

import httpx
import pytest

from release_monitor.notifier.telegram import (
    AmbiguousDeliveryError,
    DeliveryError,
    PermanentDeliveryError,
    RateLimited,
    TelegramClient,
    TransientDeliveryError,
)
from tests.fixtures_util import FAKE_TOKEN

Handler = Callable[[httpx.Request], httpx.Response]
SEND_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


def make_client(handler: Handler) -> TelegramClient:
    return TelegramClient(FAKE_TOKEN, httpx.Client(transport=httpx.MockTransport(handler)))


def telegram_json(status: int, body: dict[str, object], **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def error_body(code: int, description: str, **extra: object) -> dict[str, object]:
    return {"ok": False, "error_code": code, "description": description, **extra}


def test_send_message_returns_the_message_id_and_posts_html_without_previews() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return telegram_json(200, {"ok": True, "result": {"message_id": 4711}})

    message_id = make_client(handler).send_message("@mc_test_channel", "<b>hi</b>")

    assert message_id == 4711
    (request,) = seen
    assert str(request.url) == SEND_URL
    assert request.method == "POST"
    body = json.loads(request.content)
    assert body["chat_id"] == "@mc_test_channel"
    assert body["text"] == "<b>hi</b>"
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}


def test_get_me_returns_the_bot_description() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/getMe")
        return telegram_json(200, {"ok": True, "result": {"id": 1, "username": "release_bot"}})

    assert make_client(handler).get_me()["username"] == "release_bot"


def test_rate_limit_uses_the_retry_after_parameter() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = error_body(429, "Too Many Requests: retry after 7", parameters={"retry_after": 7})
        return telegram_json(429, body)

    with pytest.raises(RateLimited) as caught:
        make_client(handler).send_message("@c", "x")
    assert caught.value.retry_after == 7


def test_rate_limit_falls_back_to_the_header_then_a_default() -> None:
    def with_header(_request: httpx.Request) -> httpx.Response:
        return telegram_json(429, error_body(429, "Too Many Requests"), **{"Retry-After": "12"})

    def bare(_request: httpx.Request) -> httpx.Response:
        return telegram_json(429, error_body(429, "Too Many Requests"))

    with pytest.raises(RateLimited) as header:
        make_client(with_header).send_message("@c", "x")
    with pytest.raises(RateLimited) as default:
        make_client(bare).send_message("@c", "x")
    assert header.value.retry_after == 12
    assert default.value.retry_after > 0


@pytest.mark.parametrize(
    ("status", "description", "hint"),
    [
        (400, "Bad Request: chat not found", "chat id"),
        (400, "Bad Request: can't parse entities", "formatting"),
        (401, "Unauthorized", "token"),
        (403, "Forbidden: bot is not a member of the channel chat", "administrator"),
        (404, "Not Found", "token"),
    ],
)
def test_client_errors_are_permanent_with_a_helpful_hint(
    status: int, description: str, hint: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return telegram_json(status, error_body(status, description))

    with pytest.raises(PermanentDeliveryError) as caught:
        make_client(handler).send_message("@c", "x")
    assert hint in str(caught.value).lower()
    assert description in str(caught.value)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_transient(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream trouble")

    with pytest.raises(TransientDeliveryError):
        make_client(handler).send_message("@c", "x")


def test_ok_false_on_a_200_is_permanent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return telegram_json(200, error_body(400, "Bad Request: nope"))

    with pytest.raises(PermanentDeliveryError):
        make_client(handler).send_message("@c", "x")


def test_an_unreadable_success_response_is_ambiguous() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy page</html>")

    with pytest.raises(AmbiguousDeliveryError):
        make_client(handler).send_message("@c", "x")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("refused"), TransientDeliveryError),
        (httpx.ConnectTimeout("slow connect"), TransientDeliveryError),
        (httpx.PoolTimeout("busy"), TransientDeliveryError),
        (httpx.ReadTimeout("no answer"), AmbiguousDeliveryError),
        (httpx.WriteTimeout("stuck"), AmbiguousDeliveryError),
        (httpx.ReadError("reset"), AmbiguousDeliveryError),
        (httpx.RemoteProtocolError("dropped"), AmbiguousDeliveryError),
    ],
)
def test_network_failures_are_classified_by_whether_the_request_was_sent(
    error: httpx.TransportError, expected: type[DeliveryError]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(expected):
        make_client(handler).send_message("@c", "x")


def test_ambiguous_sends_are_never_retried_inside_the_client() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("no answer")

    with pytest.raises(AmbiguousDeliveryError):
        make_client(handler).send_message("@c", "x")
    assert len(calls) == 1


# -- token safety -----------------------------------------------------------------------

LEAKY = f"failed for {SEND_URL} with token {FAKE_TOKEN}"


def _scenarios() -> list[Handler]:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(LEAKY)

    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(LEAKY)

    def echoes_url(_request: httpx.Request) -> httpx.Response:
        return telegram_json(400, error_body(400, f"Bad Request: see {SEND_URL}"))

    def server(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=LEAKY)

    def rate(_request: httpx.Request) -> httpx.Response:
        return telegram_json(429, error_body(429, LEAKY, parameters={"retry_after": 1}))

    return [timeout, refused, echoes_url, server, rate]


@pytest.mark.parametrize("handler", _scenarios())
def test_the_token_never_appears_in_errors_or_logs(
    handler: Handler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DeliveryError) as caught:
        make_client(handler).send_message("@c", "x")

    error = caught.value
    rendered = [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception(error)),
    ]
    for text in (*rendered, caplog.text):
        assert FAKE_TOKEN not in text
        assert "AAFAKE" not in text
    assert error.__cause__ is None


def test_the_client_repr_hides_the_token() -> None:
    client = make_client(lambda _request: httpx.Response(200))
    assert FAKE_TOKEN not in repr(client)
    assert FAKE_TOKEN not in str(client)


def test_an_empty_token_is_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="token"):
        TelegramClient("", httpx.Client())
