"""Exercise final console output and transport behaviour from the logging reviews."""

import gzip
import io
import logging
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from app.bedolaga.client import BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer, _ReplyBackoff
from app.bot.sender import TelegramMessageSender
from app.llm.mcp_client import HttpMcpClient
from app.logging_config import TRACE, log_failure, setup_logging
from app.logging_context import operation_context
from app.logging_http import create_logging_hooks
from app.logging_redaction import register_secret
from app.retry import post_with_retry

PRIVATE_TEXT = "alice@example.test PRIVATE_MESSAGE"
PRIVATE_ID = 987654321


@pytest.mark.parametrize("level", ["TRACE", "INFO", "ERROR"])
def test_safe_error_parameters_survive_formatting(level: str) -> None:
    out = io.StringIO()
    setup_logging(level, out)
    logger = logging.getLogger("app.review")
    logger.error("OpenAI API error (model=%s, status=%d)", "test-model", 429)
    try:
        raise ValueError(PRIVATE_TEXT)
    except ValueError as exc:
        with operation_context("op-review"):
            log_failure(
                logger, "Request failed", exc, status_code=429, details={"user_id": PRIVATE_ID}
            )
    error_lines = "\n".join(line for line in out.getvalue().splitlines() if "[ERROR]" in line)
    assert "model=test-model, status=429" in error_lines
    assert "error_class=ValueError" in error_lines
    assert "status_code=429" in error_lines
    assert "correlation_id=op-review" in error_lines
    assert "%s" not in error_lines and "%d" not in error_lines
    assert PRIVATE_TEXT not in error_lines and str(PRIVATE_ID) not in error_lines
    assert (PRIVATE_TEXT in out.getvalue()) == (level == "TRACE")


@pytest.mark.parametrize("level", ["INFO", "ERROR"])
def test_disabled_trace_does_not_serialize_payload(level: str) -> None:
    setup_logging(level, io.StringIO())
    logger = logging.getLogger("app.new_child.review")
    assert not logger.isEnabledFor(TRACE)
    with patch(
        "app.logging_config.safe_serialize", side_effect=AssertionError("unexpected serialization")
    ):
        log_failure(logger, "Request failed", details=object())


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["TRACE", "INFO", "ERROR"])
async def test_copy_and_bedolaga_failures_visible_without_private_error_text(level: str) -> None:
    out = io.StringIO()
    setup_logging(level, out)
    bot = MagicMock()
    bot.copy_message = AsyncMock(side_effect=RuntimeError(PRIVATE_TEXT))
    assert await TelegramMessageSender(bot).copy_message(PRIVATE_ID, PRIVATE_ID, PRIVATE_ID) is None
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.ReadError(PRIVATE_TEXT)
    assert (
        await BedolagaClient("https://example.test", "key", client).get_ticket(PRIVATE_ID) is None
    )
    error_lines = "\n".join(line for line in out.getvalue().splitlines() if "[ERROR]" in line)
    assert "Telegram message copying failed" in error_lines
    assert "Bedolaga ticket read failed" in error_lines
    assert "ReadError" in error_lines and "RuntimeError" in error_lines
    assert PRIVATE_TEXT not in error_lines and str(PRIVATE_ID) not in error_lines
    assert (PRIVATE_TEXT in out.getvalue()) == (level == "TRACE")


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["TRACE", "INFO", "ERROR"])
async def test_mcp_is_error_retains_server_and_tool_but_not_content(level: str) -> None:
    out = io.StringIO()
    setup_logging(level, out)
    sdk = AsyncMock()
    sdk.call_tool.return_value = CallToolResult(
        is_error=True, content=[TextContent(type="text", text=PRIVATE_TEXT)]
    )
    client = HttpMcpClient("bedolaga", "https://example.test")
    await client._invoke_tool(sdk, "lookup", {"user_id": PRIVATE_ID})
    error_lines = "\n".join(line for line in out.getvalue().splitlines() if "[ERROR]" in line)
    assert "server=bedolaga, tool=lookup" in error_lines
    assert PRIVATE_TEXT not in error_lines and str(PRIVATE_ID) not in error_lines


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["TRACE", "INFO", "ERROR"])
async def test_ticket_progress_identifiers_are_trace_only(level: str) -> None:
    out = io.StringIO()
    setup_logging(level, out)
    dependencies = {
        name: MagicMock()
        for name in (
            "client",
            "llm_client",
            "state",
            "rate_limiter",
            "admin_notifier",
            "forwarder",
            "knowledge_gap_service",
            "conversation_state",
        )
    }
    answerer = TicketAnswerer(**dependencies)
    answerer._reply_backoff[PRIVATE_ID] = _ReplyBackoff(2, time.monotonic() + 60)
    assert answerer.backing_off(PRIVATE_ID)
    answerer.client.get_ticket = AsyncMock(
        return_value=MagicMock(last_user_message=MagicMock(id=PRIVATE_ID))
    )
    await answerer.schedule_newer_user_message(PRIVATE_ID, 1)
    assert (str(PRIVATE_ID) in out.getvalue()) == (level == "TRACE")


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.reads = 0
        self.closes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type", ["text/plain", "text/event-stream", "application/octet-stream"]
)
async def test_stream_is_not_read_before_caller_and_closes_once(content_type: str) -> None:
    out = io.StringIO()
    setup_logging("TRACE", out)
    body = ResponseStream([b"first", b"second"])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": content_type}, stream=body)
    )
    async with httpx.AsyncClient(transport=transport, event_hooks=create_logging_hooks()) as client:
        async with client.stream("GET", "https://example.test") as response:
            assert body.reads == 0
            iterator = response.aiter_raw()
            assert await anext(iterator) == b"first"
            assert body.reads == 1
            await iterator.aclose()
    assert body.closes == 1
    assert "first" not in out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["TRACE", "INFO", "ERROR"])
async def test_read_error_retains_identity_and_retry_behaviour(level: str) -> None:
    out = io.StringIO()
    setup_logging(level, out)
    error = httpx.ReadError(PRIVATE_TEXT)
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ResponseStream([b'{"ok":true}'], error if calls == 1 else None),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), event_hooks=create_logging_hooks()
    ) as client:
        with pytest.raises(httpx.ReadError) as caught:
            await client.get("https://example.test")
        assert caught.value is error
        calls = 0
        response = await post_with_retry(client, "https://example.test", attempts=2)
        assert response.json() == {"ok": True}
        assert calls == 2
    errors = "\n".join(line for line in out.getvalue().splitlines() if "[ERROR]" in line)
    assert "HTTP attempt failed" in errors and "ReadError" in errors
    assert PRIVATE_TEXT not in errors


@pytest.mark.asyncio
async def test_complete_compressed_json_is_logged_and_credentials_across_chunks_redacted() -> None:
    out = io.StringIO()
    setup_logging("TRACE", out)
    secret = "unique-private-credential-value"
    register_secret(secret)
    raw = ('{"text":"' + PRIVATE_TEXT + '", "token":"' + secret + '"}').encode()
    compressed = gzip.compress(raw)
    body = ResponseStream([compressed[:15], compressed[15:]])
    headers = {"content-type": "application/json", "content-encoding": "gzip"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=body)),
        event_hooks=create_logging_hooks(),
    ) as client:
        response = await client.get("https://example.test")
    assert response.content == raw
    assert PRIVATE_TEXT in out.getvalue()
    assert secret not in out.getvalue()
    assert body.closes == 1
