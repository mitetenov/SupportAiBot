"""Unit tests for shared HTTP transport logging (app/logging_http.py)."""

import io
from collections.abc import AsyncGenerator, Iterator

import httpx
import pytest

from app.logging_config import setup_logging
from app.logging_context import clear_context, operation_context, request_context
from app.logging_http import (
    LoggingTransport,
    create_logging_hooks,
    sanitize_headers,
    sanitize_url,
)
from app.retry import post_with_retry


@pytest.fixture(autouse=True)
def _reset_logging_and_context() -> Iterator[None]:
    clear_context()
    yield
    clear_context()
    setup_logging(level="INFO")


class TestSanitization:
    """Verify URL and header sanitization in HTTP transport logging."""

    def test_sanitize_url_scrubs_userinfo_and_sensitive_query(self) -> None:
        raw_url = "https://user:secretpass@example.com/api?token=secret123&api_key=mykey&page=1"
        sanitized = sanitize_url(raw_url)
        assert "secretpass" not in sanitized
        assert "secret123" not in sanitized
        assert "mykey" not in sanitized
        assert "user:[REDACTED]@example.com" in sanitized
        assert "token=[REDACTED]" in sanitized
        assert "api_key=[REDACTED]" in sanitized
        assert "page=1" in sanitized

    def test_sanitize_headers_scrubs_credentials(self) -> None:
        headers = {
            "Authorization": "Bearer sk-proj-supersecrettoken12345",
            "X-API-Key": "my-secret-key-12345",
            "Cookie": "session=abc123456; token=xyz",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        sanitized = sanitize_headers(headers)
        assert sanitized["Authorization"] == "[REDACTED]"
        assert sanitized["X-API-Key"] == "[REDACTED]"
        assert sanitized["Cookie"] == "[REDACTED]"
        assert sanitized["Content-Type"] == "application/json"
        assert sanitized["Accept"] == "application/json"


class TestTransportLoggingTrace:
    """Verify paired TRACE records for HTTP requests and responses."""

    @pytest.mark.asyncio
    async def test_trace_logs_paired_request_and_response_with_ids_and_duration(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "items": [1, 2, 3]})

        transport = LoggingTransport(httpx.MockTransport(mock_handler))
        client = httpx.AsyncClient(transport=transport)

        with operation_context("op-trace-123"):
            with request_context("req-trace-456", attempt=1):
                response = await client.post(
                    "https://api.example.com/v1/query?token=secret99",
                    headers={"Authorization": "Bearer topsecret", "X-Custom": "safe-val"},
                    json={"query": "test query"},
                )

        assert response.status_code == 200
        output = stream.getvalue()

        # Check request line
        assert "HTTP request: POST" in output
        assert "https://api.example.com/v1/query?token=[REDACTED]" in output
        assert "topsecret" not in output
        assert "token=secret99" not in output
        assert "test query" in output  # Request body present in TRACE
        assert "attempt 1" in output or "attempt=1" in output
        assert "req-trace-456" in output
        assert "op-trace-123" in output

        # Check response line
        assert "HTTP response: POST" in output
        assert "-> 200" in output
        assert "items" in output  # Response body present in TRACE
        assert "req-trace-456" in output

    @pytest.mark.asyncio
    async def test_trace_preserves_streaming_responses_without_consuming(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        class CustomStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.chunks = [b"chunk-one-", b"chunk-two"]
                self.read_count = 0

            async def __aiter__(self) -> AsyncGenerator[bytes]:
                for c in self.chunks:
                    self.read_count += 1
                    yield c

        body = CustomStream()

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body, headers={"Content-Type": "text/plain"})

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        async with client.stream("GET", "https://api.example.com/stream") as resp:
            assert body.read_count == 0
            content = bytearray()
            async for chunk in resp.aiter_bytes():
                content.extend(chunk)

        assert bytes(content) == b"chunk-one-chunk-two"
        output = stream.getvalue()
        assert "HTTP response: GET" in output
        assert "chunk-one-chunk-two" in output

    @pytest.mark.asyncio
    async def test_trace_handles_streaming_request_body_safely(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        class CustomUploadStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncGenerator[bytes]:
                yield b"upload-data"

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"uploaded": True})

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        req = client.build_request(
            "POST", "https://api.example.com/upload", content=CustomUploadStream()
        )
        resp = await client.send(req)
        assert resp.status_code == 200
        output = stream.getvalue()
        assert "HTTP request: POST" in output
        assert "[streaming request body]" in output or "streaming" in output

    @pytest.mark.asyncio
    async def test_trace_handles_binary_bodies_with_metadata(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        binary_payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=binary_payload, headers={"Content-Type": "image/png"}
            )

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        resp = await client.post("https://api.example.com/image", content=binary_payload)
        assert resp.status_code == 200
        output = stream.getvalue()
        assert "[binary body:" in output


class TestTransportLoggingInfoAndError:
    """Verify that under INFO and ERROR bodies are NEVER serialized or logged."""

    @pytest.mark.asyncio
    async def test_info_level_does_not_log_request_or_response_bodies(self) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        secret_body_marker = "SUPER_SECRET_PAYLOAD_BODY_CONTENT_12345"
        secret_resp_marker = "SUPER_SECRET_RESPONSE_BODY_CONTENT_67890"

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"secret": secret_resp_marker})

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        resp = await client.post(
            "https://api.example.com/v1/test",
            json={"data": secret_body_marker},
        )
        assert resp.status_code == 200
        output = stream.getvalue()

        # Bodies must NOT appear in output under INFO
        assert secret_body_marker not in output
        assert secret_resp_marker not in output

    @pytest.mark.asyncio
    async def test_error_level_does_not_log_request_or_response_bodies(self) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        secret_body_marker = "ERROR_SECRET_REQUEST_BODY_XYZ"
        secret_resp_marker = "ERROR_SECRET_RESPONSE_BODY_XYZ"

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": secret_resp_marker})

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        resp = await client.post(
            "https://api.example.com/v1/fail",
            json={"data": secret_body_marker},
        )
        assert resp.status_code == 500
        output = stream.getvalue()

        assert secret_body_marker not in output
        assert secret_resp_marker not in output


class TestRetryIntegration:
    """Verify attempt numbering and retry preservation in post_with_retry."""

    @pytest.mark.asyncio
    async def test_post_with_retry_tracks_attempts_and_ids(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        calls = 0

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(503, json={"error": "temporarily unavailable"})
            return httpx.Response(200, json={"success": True})

        client = httpx.AsyncClient(
            event_hooks=create_logging_hooks(),
            transport=httpx.MockTransport(mock_handler),
        )

        with operation_context("op-retry-test"):
            resp = await post_with_retry(
                client,
                "https://api.example.com/retry-endpoint",
                json={"req": "data"},
                attempts=3,
                base_delay=0.01,
            )

        assert resp.status_code == 200
        assert calls == 3
        output = stream.getvalue()

        assert "attempt 1" in output or "attempt=1" in output
        assert "attempt 2" in output or "attempt=2" in output
        assert "attempt 3" in output or "attempt=3" in output
        assert "op-retry-test" in output

    @pytest.mark.asyncio
    async def test_transport_exception_propagation_and_logging(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        def mock_handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("Connection timed out")

        transport = LoggingTransport(httpx.MockTransport(mock_handler))
        client = httpx.AsyncClient(transport=transport)

        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://api.example.com/timeout")

        output = stream.getvalue()
        assert "HTTP request failed" in output or "ConnectTimeout" in output
