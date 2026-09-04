"""Async-safe logging context management for correlation IDs and paired request IDs."""

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_attempt_id_var: ContextVar[int | None] = ContextVar("attempt_id", default=None)


def generate_correlation_id() -> str:
    """Generate a random correlation ID representing a single operation."""
    return f"op-{secrets.token_hex(6)}"


def generate_request_id() -> str:
    """Generate a random request ID for outbound calls or individual attempts."""
    return f"req-{secrets.token_hex(6)}"


def get_correlation_id() -> str | None:
    """Return the current operation/correlation ID or None if not set."""
    return _correlation_id_var.get()


def get_request_id() -> str | None:
    """Return the current request ID or None if not set."""
    return _request_id_var.get()


def get_attempt_id() -> int | None:
    """Return the current attempt number or None if not set."""
    return _attempt_id_var.get()


def set_correlation_id(cid: str) -> Token[str | None]:
    """Manually set correlation ID in the current context, returning a reset token."""
    return _correlation_id_var.set(cid)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Reset correlation ID using the token from set_correlation_id."""
    _correlation_id_var.reset(token)


def clear_context() -> None:
    """Clear all logging context variables in the current context."""
    _correlation_id_var.set(None)
    _request_id_var.set(None)
    _attempt_id_var.set(None)


def get_logging_context() -> dict[str, Any]:
    """Return a dictionary of currently active diagnostic context variables."""
    ctx: dict[str, Any] = {}
    cid = get_correlation_id()
    if cid is not None:
        ctx["correlation_id"] = cid
    rid = get_request_id()
    if rid is not None:
        ctx["request_id"] = rid
    aid = get_attempt_id()
    if aid is not None:
        ctx["attempt_id"] = aid
    return ctx


@contextmanager
def operation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Scoped context manager for an operation.

    Sets the given correlation ID (or generates a random one) and guarantees
    cleanup upon completion, error, or cancellation.
    """
    cid = correlation_id if correlation_id is not None else generate_correlation_id()
    token = _correlation_id_var.set(cid)
    try:
        yield cid
    finally:
        _correlation_id_var.reset(token)


@contextmanager
def request_context(request_id: str | None = None, attempt: int | None = None) -> Iterator[str]:
    """Scoped context manager for a single request / attempt."""
    rid = request_id if request_id is not None else generate_request_id()
    r_token = _request_id_var.set(rid)
    a_token = _attempt_id_var.set(attempt)
    try:
        yield rid
    finally:
        _request_id_var.reset(r_token)
        _attempt_id_var.reset(a_token)
