import io
import json
import logging
import re

import httpx
import pytest

from release_monitor.logging_config import RedactionFilter, configure_logging

TOKEN = "123456:AAFAKE-token_value"
TOKEN_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_token_never_appears_in_message_args_or_exception(stream: io.StringIO) -> None:
    configure_logging("DEBUG", "text", secrets=[TOKEN], stream=stream)
    log = logging.getLogger("release_monitor.test")
    log.info("plain %s", TOKEN)
    log.info("url %s", TOKEN_URL)
    log.info("dict %(u)s", {"u": TOKEN_URL})
    try:
        raise RuntimeError(f"failed calling {TOKEN_URL}")
    except RuntimeError:
        log.exception("boom")
    out = stream.getvalue()
    assert TOKEN not in out
    assert "AAFAKE" not in out
    assert "api.telegram.org" in out


def test_pattern_redaction_without_known_secret(stream: io.StringIO) -> None:
    configure_logging("INFO", "text", secrets=[], stream=stream)
    logging.getLogger("x").info(
        "calling %s", "https://api.telegram.org/bot987654:ZZ_other-Token/getMe"
    )
    assert "ZZ_other" not in stream.getvalue()


def test_httpx_logs_do_not_leak_token(stream: io.StringIO) -> None:
    configure_logging("DEBUG", "text", secrets=[TOKEN], stream=stream)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.post(TOKEN_URL, data={"chat_id": "@x"})
    assert TOKEN not in stream.getvalue()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_httpx_status_error_message_is_redacted(stream: io.StringIO) -> None:
    configure_logging("DEBUG", "text", secrets=[TOKEN], stream=stream)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = client.post(TOKEN_URL)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logging.getLogger("x").exception("telegram failed")
    assert TOKEN not in stream.getvalue()


def test_text_format_has_utc_iso_timestamp(stream: io.StringIO) -> None:
    configure_logging("INFO", "text", secrets=[], stream=stream)
    logging.getLogger("release_monitor.app").info("hello")
    line = stream.getvalue().strip()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ", line)
    assert "INFO" in line
    assert "release_monitor.app" in line
    assert "hello" in line


def test_json_format_fields_and_extras(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", secrets=[TOKEN], stream=stream)
    logging.getLogger("release_monitor.app").info(
        "published %s", "26.3", extra={"source": "minecraft", "version": "26.3", "note": TOKEN}
    )
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "INFO"
    assert record["logger"] == "release_monitor.app"
    assert record["msg"] == "published 26.3"
    assert record["source"] == "minecraft"
    assert record["version"] == "26.3"
    assert record["ts"].endswith("Z")
    assert TOKEN not in stream.getvalue()


def test_json_format_includes_redacted_exception(stream: io.StringIO) -> None:
    configure_logging("INFO", "json", secrets=[TOKEN], stream=stream)
    try:
        raise ValueError(TOKEN_URL)
    except ValueError:
        logging.getLogger("x").exception("oops")
    record = json.loads(stream.getvalue().strip())
    assert "ValueError" in record["exc"]
    assert TOKEN not in stream.getvalue()


def test_level_is_applied(stream: io.StringIO) -> None:
    configure_logging("WARNING", "text", secrets=[], stream=stream)
    logging.getLogger("x").info("hidden")
    logging.getLogger("x").warning("shown")
    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


def test_redaction_filter_ignores_empty_secrets() -> None:
    flt = RedactionFilter(["", "  "])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg %s", ("a",), None)
    assert flt.filter(record) is True
    assert record.getMessage() == "msg a"
