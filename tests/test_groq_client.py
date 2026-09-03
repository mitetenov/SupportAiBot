"""Tests for the OpenAI-compatible Groq chat-completions client."""

from unittest.mock import MagicMock

import httpx
import pytest

from app.config import Settings
from app.llm.base import LlmProcessingException
from app.llm.groq import GroqClient
from app.llm.mcp_router import McpRouter


def make_settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "test-token",
        "telegram_support_group_chat_id": -1001234567890,
        "llm_provider": "groq",
        "embedding_provider": "gemini",
        "gemini_api_key": "gemini-test-key",
        "groq_api_key": "gsk-test-key",
        "groq_model": "llama-3.3-70b-versatile",
    }
    values.update(overrides)
    return Settings(**values)


def make_client(settings=None, http_client=None) -> GroqClient:
    router = MagicMock(spec=McpRouter)
    router.list_tools.return_value = []
    return GroqClient(
        settings or make_settings(),
        router,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        http_client=http_client,
    )


def test_builds_groq_chat_completion_request():
    client = make_client()
    body = client.build_request_body([{"role": "user", "content": "hello"}])
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["messages"][0]["content"] == "hello"
    assert body["temperature"] == 0.3


def test_parses_text_response():
    client = make_client()
    response = client.parse_response({"choices": [{"message": {"content": "answer"}}]})
    assert response.text == "answer"
    assert not response.tool_calls


def test_parses_tool_call_response():
    client = make_client()
    response = client.parse_response({
        "choices": [{"message": {"content": "", "tool_calls": [{
            "id": "call-1", "function": {"name": "nodes_list", "arguments": "{}"}
        }]}}]
    })
    assert response.tool_calls[0].name == "nodes_list"
    assert response.tool_calls[0].id == "call-1"


@pytest.mark.asyncio
async def test_maps_http_errors_without_exposing_credentials():
    async def handler(_request):
        return httpx.Response(429, json={"error": "rate limited"})

    client = make_client(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(LlmProcessingException) as exc:
            await client.call_api([], "", 1)
        assert "gsk-test-key" not in str(exc.value)
        assert "Попробуйте позже" in exc.value.user_friendly_message
    finally:
        await client.http_client.aclose()
