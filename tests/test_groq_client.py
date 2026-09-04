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


@pytest.mark.parametrize("has_tools", [False, True])
@pytest.mark.parametrize(
    "model,effort,native,format_key,format_value",
    [
        ("openai/gpt-oss-20b", "none", "low", "include_reasoning", False),
        ("openai/gpt-oss-120b", "minimal", "low", "include_reasoning", False),
        ("openai/gpt-oss-120b", "medium", "medium", "include_reasoning", False),
        ("openai/gpt-oss-safeguard-20b", "max", "high", "include_reasoning", False),
        ("qwen/qwen3-32b", "none", "none", "reasoning_format", "hidden"),
        ("qwen/qwen3-32b", "high", "default", "reasoning_format", "hidden"),
        ("qwen/qwen3.6-27b", "none", "none", "reasoning_format", "hidden"),
        ("qwen/qwen3.6-27b", "max", "default", "reasoning_format", "hidden"),
        ("qwen/qwen3.8-27b", "none", "none", "reasoning_format", "hidden"),
        ("qwen/qwen3.8-27b", "minimal", "low", "reasoning_format", "hidden"),
        ("qwen/qwen3.8-27b", "medium", "medium", "reasoning_format", "hidden"),
        ("qwen/qwen3.8-27b", "xhigh", "high", "reasoning_format", "hidden"),
    ],
)
def test_native_reasoning_parameters(model, effort, native, format_key, format_value, has_tools):
    client = make_client(make_settings(groq_model=model, reasoning_effort=effort))
    if has_tools:
        client.tool_definitions = [{"type": "function", "function": {"name": "nodes_list"}}]
    body = client.build_request_body([])
    assert body["reasoning_effort"] == native
    assert body[format_key] == format_value
    assert "thinking" not in body
    assert not ({"include_reasoning", "reasoning_format"} <= body.keys())
    if has_tools:
        assert body["tool_choice"] == "auto"


@pytest.mark.parametrize("model", ["llama-3.3-70b-versatile", "custom-model", "deepseek-v4-pro"])
def test_unknown_reasoning_controls_are_not_sent(model):
    client = make_client(make_settings(groq_model=model, reasoning_effort="high"))
    body = client.build_request_body([])
    assert (
        not {"thinking", "reasoning_effort", "reasoning_format", "include_reasoning"} & body.keys()
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        ("<think>private</think>Answer", "Answer"),
        ("<think>private\nreasoning</think>\nAnswer", "Answer"),
        ("<think>unfinished private reasoning", ""),
        ("Answer", "Answer"),
    ],
)
def test_raw_thinking_never_becomes_reply_text(content, expected):
    response = make_client().parse_response({"choices": [{"message": {"content": content}}]})
    assert response.text == expected


def test_parsed_reasoning_is_separate_from_reply_and_tool_history():
    client = make_client()
    response = client.parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "Answer",
                        "reasoning": "private",
                        "tool_calls": [
                            {"id": "one", "function": {"name": "nodes_list", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        }
    )
    assert response.text == "Answer"
    assert response.reasoning_content == "private"
    conversation = []
    client.add_tool_calls_to_conversation(conversation, response)
    assert "reasoning_content" not in conversation[0]
    assert "reasoning" not in conversation[0]


def test_parses_tool_call_response():
    client = make_client()
    response = client.parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "call-1", "function": {"name": "nodes_list", "arguments": "{}"}}
                        ],
                    }
                }
            ]
        }
    )
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
