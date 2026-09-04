"""Shared HTTP transport logging hooks and transport wrappers for httpx."""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.logging_config import TRACE
from app.logging_context import (
    generate_request_id,
    get_attempt_id,
    get_correlation_id,
    get_request_id,
)
from app.logging_redaction import is_sensitive_key, redact_credentials_in_text

logger = logging.getLogger("app.http")


def sanitize_url(url: httpx.URL | str) -> str:
    """Sanitize URL by redacting credentials in userinfo and sensitive query parameters."""
    return redact_credentials_in_text(str(url))


def sanitize_headers(headers: Mapping[str, str] | httpx.Headers) -> dict[str, str]:
    """Sanitize headers by replacing sensitive header values with [REDACTED]."""
    sanitized: dict[str, str] = {}
    for k, v in headers.items():
        if is_sensitive_key(str(k)):
            sanitized[str(k)] = "[REDACTED]"
        else:
            sanitized[str(k)] = redact_credentials_in_text(str(v))
    return sanitized


def _safe_read_request_body(request: httpx.Request) -> str | None:
    """Read request body safely for TRACE logging without consuming unread streams."""
    if not logger.isEnabledFor(TRACE):
        return None
    try:
        raw = request.content
    except httpx.RequestNotRead, RuntimeError, AttributeError:
        return "[streaming request body]"
    except Exception:
        return "[unavailable request body]"

    if not raw:
        return ""
    try:
        text = raw.decode("utf-8")
        return redact_credentials_in_text(text)
    except UnicodeDecodeError:
        return f"[binary body: {len(raw)} bytes]"


def _safe_read_response_body(response: httpx.Response) -> str | None:
    """Read response body safely for TRACE logging without consuming unread streams."""
    if not logger.isEnabledFor(TRACE):
        return None
    if not hasattr(response, "_content"):
        return "[streaming response]"
    raw = response._content
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8")
        return redact_credentials_in_text(text)
    except UnicodeDecodeError:
        return f"[binary body: {len(raw)} bytes]"


async def log_request(request: httpx.Request) -> None:
    """Asynchronous request hook for httpx.AsyncClient.

    Records timing, operation, request, and attempt identifiers in request.extensions
    and emits a TRACE record if TRACE logging is enabled.
    """
    corr_id = get_correlation_id()
    req_id = get_request_id() or generate_request_id()
    attempt = get_attempt_id() or 1
    start_time = time.perf_counter()

    request.extensions["http_logging"] = {
        "correlation_id": corr_id,
        "request_id": req_id,
        "attempt_id": attempt,
        "start_time": start_time,
    }

    if logger.isEnabledFor(TRACE):
        method = request.method
        sanitized_url = sanitize_url(request.url)
        sanitized_hdrs = sanitize_headers(request.headers)
        body = _safe_read_request_body(request)

        extra: dict[str, Any] = {
            "method": method,
            "url": sanitized_url,
            "attempt_id": attempt,
            "request_id": req_id,
            "headers": sanitized_hdrs,
        }
        if corr_id:
            extra["correlation_id"] = corr_id
        if body is not None and body != "":
            extra["body"] = body

        body_snippet = f" body={body}" if body is not None and body != "" else ""
        logger.log(
            TRACE,
            f"HTTP request: {method} {sanitized_url} (attempt {attempt}){body_snippet}",
            extra=extra,
        )


async def log_response(response: httpx.Response, request: httpx.Request | None = None) -> None:
    """Asynchronous response hook for httpx.AsyncClient.

    Reads paired context from response.request.extensions, computes duration,
    and emits a TRACE record with response status, duration, and body (if non-streaming).
    """
    req = getattr(response, "_request", None) or request
    meta: dict[str, Any] = req.extensions.get("http_logging", {}) if req is not None else {}
    start_time = meta.get("start_time")
    duration = (time.perf_counter() - start_time) if start_time is not None else 0.0
    req_id = meta.get("request_id") or get_request_id() or "unknown"
    attempt = meta.get("attempt_id") or get_attempt_id() or 1
    corr_id = meta.get("correlation_id") or get_correlation_id()

    if logger.isEnabledFor(TRACE):
        method = req.method if req is not None else "UNKNOWN"
        raw_url = req.url if req is not None else ""
        sanitized_url = sanitize_url(raw_url)
        status_code = response.status_code
        body = _safe_read_response_body(response)

        extra: dict[str, Any] = {
            "method": method,
            "url": sanitized_url,
            "status_code": status_code,
            "duration": f"{duration:.3f}s",
            "attempt_id": attempt,
            "request_id": req_id,
        }
        if corr_id:
            extra["correlation_id"] = corr_id
        if body is not None and body != "":
            extra["body"] = body

        body_snippet = f" body={body}" if body is not None and body != "" else ""
        logger.log(
            TRACE,
            f"HTTP response: {method} {sanitized_url} -> {status_code} ({duration:.3f}s, attempt {attempt}){body_snippet}",
            extra=extra,
        )


def create_logging_hooks() -> dict[str, list[Callable[..., Any]]]:
    """Return standard event hooks dictionary for httpx.AsyncClient."""
    return {
        "request": [log_request],
        "response": [log_response],
    }


class LoggingTransport(httpx.AsyncBaseTransport):
    """Reusable transport wrapper that logs paired request/response and transport exceptions."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await log_request(request)
        try:
            response = await self._transport.handle_async_request(request)
        except Exception as exc:
            meta = request.extensions.get("http_logging", {})
            start_time = meta.get("start_time")
            duration = (time.perf_counter() - start_time) if start_time is not None else 0.0
            req_id = meta.get("request_id") or get_request_id() or "unknown"
            attempt = meta.get("attempt_id") or get_attempt_id() or 1
            corr_id = meta.get("correlation_id") or get_correlation_id()

            if logger.isEnabledFor(TRACE):
                method = request.method
                sanitized_url = sanitize_url(request.url)
                extra: dict[str, Any] = {
                    "method": method,
                    "url": sanitized_url,
                    "attempt_id": attempt,
                    "request_id": req_id,
                    "duration": f"{duration:.3f}s",
                    "error": str(exc),
                }
                if corr_id:
                    extra["correlation_id"] = corr_id
                logger.log(
                    TRACE,
                    f"HTTP request failed: {method} {sanitized_url} ({duration:.3f}s, attempt {attempt}) "
                    f"error={exc.__class__.__name__}: {exc}",
                    extra=extra,
                )
            raise

        if getattr(response, "_request", None) is None:
            response.request = request
        await log_response(response, request=request)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def create_logged_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient configured with standard logging hooks."""
    event_hooks = kwargs.pop("event_hooks", None) or {}
    hooks = create_logging_hooks()
    if "request" in event_hooks:
        hooks["request"].extend(event_hooks["request"])
    if "response" in event_hooks:
        hooks["response"].extend(event_hooks["response"])
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
