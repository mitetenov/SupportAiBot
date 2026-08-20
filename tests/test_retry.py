"""Tests for the shared outbound-HTTP retry policy."""

from typing import Any

import httpx
import pytest

from app.retry import RETRYABLE_STATUS, backoff_delay, post_with_retry


class _RecordingClient:
    """Stands in for httpx.AsyncClient, replaying a scripted set of outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        request=httpx.Request("POST", "https://example.test/v1"),
    )


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries must be exercised without spending the backoff in real time."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.retry._sleep", instant)


class TestPostWithRetry:
    async def test_returns_a_successful_response_without_retrying(self) -> None:
        client = _RecordingClient([_response(200)])

        response = await post_with_retry(client, "https://example.test/v1")  # type: ignore[arg-type]

        assert response.status_code == 200
        assert len(client.calls) == 1

    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    async def test_retries_every_retryable_status(self, status: int) -> None:
        client = _RecordingClient([_response(status), _response(200)])

        response = await post_with_retry(client, "https://example.test/v1")  # type: ignore[arg-type]

        assert response.status_code == 200
        assert len(client.calls) == 2

    async def test_does_not_retry_a_client_error(self) -> None:
        client = _RecordingClient([_response(401)])

        response = await post_with_retry(client, "https://example.test/v1")  # type: ignore[arg-type]

        assert response.status_code == 401
        assert len(client.calls) == 1

    async def test_gives_up_after_the_attempt_budget(self) -> None:
        client = _RecordingClient([_response(429)])

        response = await post_with_retry(client, "https://example.test/v1", attempts=3)  # type: ignore[arg-type]

        assert response.status_code == 429
        assert len(client.calls) == 3

    async def test_retries_a_transport_failure_and_then_succeeds(self) -> None:
        client = _RecordingClient([httpx.ConnectError("boom"), _response(200)])

        response = await post_with_retry(client, "https://example.test/v1")  # type: ignore[arg-type]

        assert response.status_code == 200
        assert len(client.calls) == 2

    async def test_reraises_when_every_attempt_fails_to_connect(self) -> None:
        client = _RecordingClient([httpx.ConnectTimeout("nope")])

        with pytest.raises(httpx.ConnectTimeout):
            await post_with_retry(client, "https://example.test/v1", attempts=2)  # type: ignore[arg-type]

        assert len(client.calls) == 2

    async def test_honours_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("app.retry._sleep", record)
        client = _RecordingClient([_response(429, {"retry-after": "2"}), _response(200)])

        await post_with_retry(client, "https://example.test/v1")  # type: ignore[arg-type]

        assert slept == [2.0]

    async def test_passes_headers_and_body_through_unchanged(self) -> None:
        client = _RecordingClient([_response(200)])

        await post_with_retry(
            client,  # type: ignore[arg-type]
            "https://example.test/v1",
            headers={"Authorization": "Bearer x"},
            json={"model": "test"},
            timeout=12.0,
        )

        assert client.calls[0]["headers"] == {"Authorization": "Bearer x"}
        assert client.calls[0]["json"] == {"model": "test"}
        assert client.calls[0]["timeout"] == 12.0


class TestBackoff:
    def test_grows_with_the_attempt_and_stays_bounded(self) -> None:
        first = backoff_delay(0, base_delay=1.0)
        later = backoff_delay(5, base_delay=1.0)

        assert 0.5 <= first <= 1.0
        assert later <= 8.0
        assert later > first
