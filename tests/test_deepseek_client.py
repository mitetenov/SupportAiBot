"""Tests for DeepSeekClient (OpenAI-compatible completions, tool calling, image refusal)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.llm.base import LlmProcessingException, ToolCall
from app.llm.deepseek import DeepSeekClient
from app.llm.mcp_router import McpRouter
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager
from app.storage.models import LlmTokenUsage


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="test_token",
        telegram_support_group_chat_id=-1001234567890,
        llm_provider="deepseek",
        embedding_provider="gemini",
        gemini_api_key="gemini-test-key",
        deepseek_api_key="deepseek-test-key",
        deepseek_model="deepseek-chat",
        deepseek_base_url="http://localhost:9999",
        remnawave_mcp_url="http://localhost:3100",
    )


@pytest.fixture
def deepseek_client(settings: Settings):
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.list_tools.return_value = []
    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_history = AsyncMock(return_value=[])
    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)
    db_manager = MagicMock(spec=DatabaseSessionManager)

    client = DeepSeekClient(
        settings=settings,
        mcp_router=mcp_router,
        chat_history_service=chat_history_service,
        faq_embedding_service=faq_embedding_service,
        db_manager=db_manager,
    )
    return client


class TestDeepSeekClient:
    def test_supports_images_is_false(self, deepseek_client: DeepSeekClient):
        assert deepseek_client.supports_images() is False

    @pytest.mark.asyncio
    async def test_chat_with_image_raises_friendly_exception(self, deepseek_client: DeepSeekClient):
        with pytest.raises(LlmProcessingException) as exc_info:
            await deepseek_client.chat_with_image("text", 123, "base64", "image/png")
        assert (
            "DeepSeek не поддерживает обработку изображений" in exc_info.value.user_friendly_message
        )

    def test_build_initial_conversation_with_system_and_dynamic_context(
        self, deepseek_client: DeepSeekClient
    ):
        conv = deepseek_client.build_initial_conversation("Hello", 123, "FAQ content", None, None)
        assert len(conv) == 3
        assert conv[0]["role"] == "system"
        assert "Ты — техподдержка VPN-сервиса" in conv[0]["content"]

        assert conv[1]["role"] == "system"
        assert "Telegram ID: 123" in conv[1]["content"]
        assert "FAQ content" in conv[1]["content"]

        assert conv[2]["role"] == "user"
        assert conv[2]["content"] == "Hello"

    def test_parse_text_response(self, deepseek_client: DeepSeekClient):
        raw = """
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Hello, how can I help?",
                    "tool_calls": null
                }
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        response = deepseek_client.parse_response(raw)
        assert response.text == "Hello, how can I help?"
        assert not response.has_tool_calls()

    def test_parse_tool_call_response(self, deepseek_client: DeepSeekClient):
        raw = """
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "nodes_list",
                            "arguments": "{}"
                        }
                    }]
                }
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        response = deepseek_client.parse_response(raw)
        assert response.text == ""
        assert response.has_tool_calls()
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "nodes_list"
        assert response.tool_calls[0].id == "call_1"
        assert response.tool_calls[0].arguments == {}

    def test_parse_mixed_text_and_tool_call(self, deepseek_client: DeepSeekClient):
        raw = """
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Let me check that for you.",
                    "tool_calls": [{
                        "id": "call_mixed",
                        "type": "function",
                        "function": {"name": "nodes_list", "arguments": "{\\"status\\": \\"CONNECTED\\"}"}
                    }]
                }
            }]
        }
        """
        response = deepseek_client.parse_response(raw)
        assert response.text == "Let me check that for you."
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "nodes_list"
        assert response.tool_calls[0].arguments == {"status": "CONNECTED"}

    def test_add_tool_calls_to_conversation_serializes_arguments_as_json_string(
        self, deepseek_client: DeepSeekClient
    ):
        conv = []
        response = deepseek_client.parse_response("""
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "nodes_get", "arguments": "{\\"uuid\\": \\"abc-123\\"}"}
                    }]
                }
            }]
        }
        """)
        deepseek_client.add_tool_calls_to_conversation(conv, response)

        assert len(conv) == 1
        assert conv[0]["role"] == "assistant"
        tc_map = conv[0]["tool_calls"][0]
        assert tc_map["id"] == "call_1"
        assert tc_map["type"] == "function"
        assert isinstance(tc_map["function"]["arguments"], str)
        assert json.loads(tc_map["function"]["arguments"]) == {"uuid": "abc-123"}

    def test_add_tool_result_to_conversation(self, deepseek_client: DeepSeekClient):
        conv = []
        tc = ToolCall(name="get_nodes", id="call_1", arguments={})
        deepseek_client.add_tool_result_to_conversation(conv, tc, '{"nodes": []}')

        assert len(conv) == 1
        assert conv[0]["role"] == "tool"
        assert conv[0]["tool_call_id"] == "call_1"
        assert conv[0]["content"] == '{"nodes": []}'

    @pytest.mark.asyncio
    async def test_save_usage(self, deepseek_client: DeepSeekClient):
        raw = """
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        deepseek_client.db_manager.session.return_value = mock_ctx

        await deepseek_client.save_usage(raw, 123)

        assert mock_session.add.called
        usage = mock_session.add.call_args[0][0]
        assert isinstance(usage, LlmTokenUsage)
        assert usage.telegram_id == 123
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_get_provider_name(self, deepseek_client: DeepSeekClient):
        assert deepseek_client.get_provider_name() == "DeepSeek"
