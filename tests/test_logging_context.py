"""Tests for logging context management, correlation IDs, and async propagation."""

import asyncio

import pytest

from app.logging_context import (
    clear_context,
    generate_correlation_id,
    generate_request_id,
    get_attempt_id,
    get_correlation_id,
    get_logging_context,
    get_request_id,
    operation_context,
    request_context,
    reset_correlation_id,
    set_correlation_id,
)


def test_default_context_is_empty() -> None:
    clear_context()
    assert get_correlation_id() is None
    assert get_request_id() is None
    assert get_attempt_id() is None
    assert get_logging_context() == {}


def test_generate_ids_uniqueness_and_format() -> None:
    id1 = generate_correlation_id()
    id2 = generate_correlation_id()
    assert id1 != id2
    assert isinstance(id1, str)
    assert len(id1) >= 8

    req1 = generate_request_id()
    req2 = generate_request_id()
    assert req1 != req2
    assert isinstance(req1, str)
    assert len(req1) >= 8


def test_operation_context_basic_lifecycle() -> None:
    clear_context()
    assert get_correlation_id() is None

    with operation_context("test-op-123") as cid:
        assert cid == "test-op-123"
        assert get_correlation_id() == "test-op-123"
        assert get_logging_context() == {"correlation_id": "test-op-123"}

    assert get_correlation_id() is None
    assert get_logging_context() == {}


def test_operation_context_auto_generates_id() -> None:
    clear_context()
    with operation_context() as cid:
        assert cid is not None
        assert get_correlation_id() == cid
    assert get_correlation_id() is None


def test_request_context_lifecycle() -> None:
    clear_context()
    with request_context("req-abc", attempt=1) as rid:
        assert rid == "req-abc"
        assert get_request_id() == "req-abc"
        assert get_attempt_id() == 1
        assert get_logging_context() == {"request_id": "req-abc", "attempt_id": 1}

    assert get_request_id() is None
    assert get_attempt_id() is None


def test_nested_request_context_without_attempt_clears_attempt() -> None:
    clear_context()
    with request_context("req-outer", attempt=5):
        assert get_request_id() == "req-outer"
        assert get_attempt_id() == 5
        with request_context("req-inner"):
            assert get_request_id() == "req-inner"
            assert get_attempt_id() is None
        assert get_request_id() == "req-outer"
        assert get_attempt_id() == 5


def test_context_cleanup_on_exception() -> None:
    clear_context()
    with pytest.raises(ValueError, match="boom"):
        with operation_context("crash-op"):
            assert get_correlation_id() == "crash-op"
            raise ValueError("boom")

    assert get_correlation_id() is None

    with pytest.raises(RuntimeError, match="request failed"):
        with request_context("crash-req", attempt=2):
            assert get_request_id() == "crash-req"
            raise RuntimeError("request failed")

    assert get_request_id() is None
    assert get_attempt_id() is None


def test_nested_operation_context() -> None:
    clear_context()
    with operation_context("outer") as outer_id:
        assert get_correlation_id() == "outer"
        with operation_context("inner") as inner_id:
            assert get_correlation_id() == "inner"
            assert inner_id == "inner"
        assert get_correlation_id() == "outer"
        assert outer_id == "outer"
    assert get_correlation_id() is None


def test_manual_set_and_reset_correlation_id() -> None:
    clear_context()
    token = set_correlation_id("manual-cid")
    assert get_correlation_id() == "manual-cid"
    reset_correlation_id(token)
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_async_task_propagation() -> None:
    clear_context()

    async def child_task(expected_cid: str) -> bool:
        await asyncio.sleep(0.01)
        return get_correlation_id() == expected_cid

    with operation_context("parent-async-cid"):
        task = asyncio.create_task(child_task("parent-async-cid"))
        result = await task
        assert result is True

    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_isolation() -> None:
    clear_context()

    async def run_worker(name: str, delay: float) -> list[str | None]:
        history = []
        with operation_context(f"op-{name}"):
            history.append(get_correlation_id())
            await asyncio.sleep(delay)
            history.append(get_correlation_id())
        history.append(get_correlation_id())
        return history

    task_a = asyncio.create_task(run_worker("A", 0.02))
    task_b = asyncio.create_task(run_worker("B", 0.01))

    res_a, res_b = await asyncio.gather(task_a, task_b)

    assert res_a == ["op-A", "op-A", None]
    assert res_b == ["op-B", "op-B", None]


@pytest.mark.asyncio
async def test_long_running_worker_does_not_leak_stale_ids() -> None:
    clear_context()

    jobs = ["job1", "job2-fail", "job3"]
    processed: list[dict[str, str | None]] = []

    for job in jobs:
        before_cid = get_correlation_id()
        try:
            with operation_context(f"cid-{job}"):
                active_cid = get_correlation_id()
                if "fail" in job:
                    raise RuntimeError(f"error in {job}")
                processed.append({"before": before_cid, "active": active_cid})
        except RuntimeError:
            processed.append({"before": before_cid, "active": active_cid, "failed": True})  # type: ignore[dict-item]
        finally:
            after_cid = get_correlation_id()
            processed.append({"after": after_cid})

    for step in processed:
        if "before" in step:
            assert step["before"] is None
        if "after" in step:
            assert step["after"] is None
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_async_cancellation_cleanup() -> None:
    clear_context()

    started = asyncio.Event()

    async def cancellable_coro() -> None:
        with operation_context("cancel-me"):
            started.set()
            await asyncio.sleep(10.0)

    task = asyncio.create_task(cancellable_coro())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert get_correlation_id() is None
