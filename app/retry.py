"""One retry policy for every outbound HTTP call this bot makes.

A provider answering 429 or 503, or a connection dropped mid-flight, used to end
the turn: the user was told "произошла ошибка, попробуйте позже" for something
that would have succeeded a second later. Retrying is only safe because every
call site here is idempotent — an embedding request, or a chat completion the
model has not yet produced.
"""

import logging
import random
from asyncio import sleep as _sleep
from collections.abc import Mapping
from typing import Any

import httpx

from app.logging_context import request_context

logger = logging.getLogger(__name__)

#: Status codes worth sending again: rate limits, and the gateway errors that
#: mean "not right now" rather than "not ever".
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS: int = 3
DEFAULT_BASE_DELAY: float = 0.5
MAX_DELAY: float = 8.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a Retry-After header, honouring the delta-seconds form only."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return min(seconds, MAX_DELAY) if seconds >= 0 else None


def backoff_delay(attempt: int, base_delay: float = DEFAULT_BASE_DELAY) -> float:
    """Exponential backoff with jitter, so retries do not resynchronise."""
    return min(base_delay * (2**attempt), MAX_DELAY) * (0.5 + random.random() / 2)


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    timeout: float | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    description: str = "request",
) -> httpx.Response:
    """POST, retrying transport failures and retryable status codes.

    Returns the last response received. A request that never got a response
    raises the final transport error, so callers keep their existing handling
    for both outcomes.
    """
    request_kwargs: dict[str, Any] = {"headers": dict(headers) if headers else None, "json": json}
    if timeout is not None:
        request_kwargs["timeout"] = timeout

    last_error: Exception | None = None

    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        with request_context(attempt=attempt + 1):
            try:
                response = await client.post(url, **request_kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
                if is_last:
                    raise
                delay = backoff_delay(attempt, base_delay)
                logger.warning(
                    "%s failed (%s) — retrying in %.1fs (attempt %d/%d)",
                    description,
                    e.__class__.__name__,
                    delay,
                    attempt + 2,
                    attempts,
                )
                await _sleep(delay)
                continue

            if response.status_code in RETRYABLE_STATUS and not is_last:
                delay = _retry_after_seconds(response) or backoff_delay(attempt, base_delay)
                logger.warning(
                    "%s returned %d — retrying in %.1fs (attempt %d/%d)",
                    description,
                    response.status_code,
                    delay,
                    attempt + 2,
                    attempts,
                )
                await _sleep(delay)
                continue

            return response

    # Unreachable: the loop either returns a response or raises on the last
    # attempt. Kept so the function has a single, obvious contract.
    raise last_error if last_error else RuntimeError(f"{description} produced no response")
