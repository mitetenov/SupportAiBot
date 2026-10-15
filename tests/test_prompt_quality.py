import json

import httpx
import pytest

from benchmarks.prompt_quality import RequestObserver, context_sizes, summarize, usage_counts


def test_missing_cache_usage_is_not_reported_as_zero() -> None:
    assert usage_counts({"usage": {"prompt_tokens": 100, "completion_tokens": 10}}) == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": None,
    }
    assert (
        usage_counts({"usage": {"prompt_tokens_details": {"cached_tokens": 0}}})[
            "cached_input_tokens"
        ]
        == 0
    )


def test_invalid_usage_is_unknown() -> None:
    assert usage_counts({"usage": {"prompt_tokens": True, "completion_tokens": -1}}) == {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
    }


def test_context_sizes_separate_tool_results_from_conversation() -> None:
    base = {
        "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}]
    }
    with_result = {"messages": [*base["messages"], {"role": "tool", "content": "large result"}]}
    before, after = context_sizes(base), context_sizes(with_result)
    assert after["system"] == before["system"]
    assert after["conversation"] == before["conversation"]
    assert before["tool_results"] == 0 < after["tool_results"]


@pytest.mark.asyncio
async def test_observer_counts_all_calls_and_does_not_record_credentials() -> None:
    observer = RequestObserver()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"usage": {"prompt_tokens": 50, "completion_tokens": 5}}
        )
    )
    async with httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [observer.on_request], "response": [observer.on_response]},
    ) as client:
        for _ in range(2):
            await client.post(
                "https://example.test/chat",
                headers={"Authorization": "Bearer synthetic-secret"},
                json={"messages": [{"role": "user", "content": "synthetic-question"}]},
            )
    result = summarize([{"name": "case", "passed": True, "requests": observer.requests}])
    assert result["api_requests"] == 2
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": None,
    }
    assert "synthetic-secret" not in json.dumps(observer.requests)
    assert "synthetic-question" not in json.dumps(observer.requests)


def test_summary_keeps_failed_cases_and_incomplete_token_accounting() -> None:
    records = [
        {"name": "same", "passed": True, "requests": [{"input_tokens": 100}]},
        {"name": "same", "passed": False, "requests": [{"status": 503}]},
    ]
    result = summarize(records)
    assert result["cases"] == {"same": {"passed": 1, "runs": 2}}
    assert result["usage"]["input_tokens"] is None
