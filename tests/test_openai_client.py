"""Tests for OpenAiClient (OpenAI Responses API, tool calling, vision support)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.llm.base import ToolCall
from app.llm.mcp_client import McpTool
from app.llm.mcp_router import McpRouter
from app.llm.openai_client import OpenAiClient
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager
from app.storage.models import LlmTokenUsage


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="test_token",
        telegram_support_group_chat_id=-1001234567890,
        llm_provider="openai",
        embedding_provider="openai",
        openai_api_key="sk-openai-test-key",
        openai_model="gpt-5.6-luna",
        openai_base_url="http://localhost:9999",
        remnawave_mcp_url="http://localhost:3100",
    )


@pytest.fixture
def openai_client(settings: Settings):
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.list_tools.return_value = []
    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_history = AsyncMock(return_value=[])
    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)
    db_manager = MagicMock(spec=DatabaseSessionManager)

    client = OpenAiClient(
        settings=settings,
        mcp_router=mcp_router,
        chat_history_service=chat_history_service,
        faq_embedding_service=faq_embedding_service,
        db_manager=db_manager,
    )
    return client


class TestOpenAiClient:
    def test_supports_images_is_true(self, openai_client: OpenAiClient):
        assert openai_client.supports_images() is True

    def test_reject_null_or_blank_api_key(self, settings: Settings, openai_client: OpenAiClient):
        settings.openai_api_key = None
        with pytest.raises(ValueError) as exc_info:
            OpenAiClient(
                settings=settings,
                mcp_router=openai_client.mcp_router,
                chat_history_service=openai_client.chat_history_service,
                faq_embedding_service=openai_client.faq_embedding_service,
            )
        assert "OpenAI API key must not be null or blank" in str(exc_info.value)

    def test_build_initial_conversation_with_text(self, openai_client: OpenAiClient):
        conv = openai_client.build_initial_conversation("Hello", 123, "FAQ content", None, None)
        assert len(conv) == 3
        assert conv[0]["role"] == "system"
        assert "Ты — техподдержка VPN-сервиса" in conv[0]["content"]

        assert conv[1]["role"] == "system"
        assert "Telegram ID: 123" in conv[1]["content"]
        assert "FAQ content" in conv[1]["content"]

        assert conv[2]["role"] == "user"
        assert conv[2]["content"] == "Hello"

    def test_build_initial_conversation_with_image(self, openai_client: OpenAiClient):
        conv = openai_client.build_initial_conversation(
            "Describe this", 123, None, "base64data", "image/png"
        )
        assert len(conv) == 3
        user_msg = conv[2]
        assert user_msg["role"] == "user"
        parts = user_msg["content"]
        assert len(parts) == 2
        assert parts[0]["type"] == "input_text"
        assert parts[0]["text"] == "Describe this"
        assert parts[1]["type"] == "input_image"
        assert parts[1]["image_url"] == "data:image/png;base64,base64data"

    def test_build_initial_conversation_with_image_only(self, openai_client: OpenAiClient):
        conv = openai_client.build_initial_conversation("", 123, None, "base64data", "image/jpeg")
        assert len(conv) == 3
        user_msg = conv[2]
        parts = user_msg["content"]
        assert len(parts) == 1
        assert parts[0]["type"] == "input_image"
        assert parts[0]["image_url"] == "data:image/jpeg;base64,base64data"

    def test_build_request_body_responses_api(self, openai_client: OpenAiClient):
        openai_client.mcp_router.list_tools.return_value = [
            McpTool("nodes_list", "List all nodes", {"type": "object", "properties": {}})
        ]
        client_with_tools = OpenAiClient(
            settings=openai_client.settings,
            mcp_router=openai_client.mcp_router,
            chat_history_service=openai_client.chat_history_service,
            faq_embedding_service=openai_client.faq_embedding_service,
        )

        body = client_with_tools.build_request_body([{"role": "user", "content": "hello"}])
        assert body["model"] == "gpt-5.6-luna"
        assert body["input"] == [{"role": "user", "content": "hello"}]
        assert len(body["tools"]) == 1
        assert body["tools"][0]["name"] == "nodes_list"
        assert body["tool_choice"] == "auto"
        assert body["reasoning"] == {"effort": "none"}

    def test_parse_text_response(self, openai_client: OpenAiClient):
        raw = """
        {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "Hello, how can I help?"
                }]
            }],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        response = openai_client.parse_response(raw)
        assert response.text == "Hello, how can I help?"
        assert not response.has_tool_calls()

    def test_parse_tool_call_response(self, openai_client: OpenAiClient):
        raw = """
        {
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "nodes_list",
                "arguments": "{}"
            }],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        response = openai_client.parse_response(raw)
        assert response.text == ""
        assert response.has_tool_calls()
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "nodes_list"
        assert response.tool_calls[0].id == "call_1"
        assert response.tool_calls[0].arguments == {}

    def test_add_tool_calls_to_conversation(self, openai_client: OpenAiClient):
        conv = []
        openai_client.add_tool_calls_to_conversation(
            conv,
            openai_client.parse_response("""
        {
            "output": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "nodes_get",
                "arguments": "{\\"uuid\\": \\"abc-123\\"}"
            }]
        }
        """),
        )

        assert len(conv) == 1
        assert conv[0]["type"] == "function_call"
        assert conv[0]["call_id"] == "call_1"
        assert conv[0]["name"] == "nodes_get"
        assert json.loads(conv[0]["arguments"]) == {"uuid": "abc-123"}

    def test_add_tool_result_to_conversation(self, openai_client: OpenAiClient):
        conv = []
        tc = ToolCall(name="get_nodes", id="call_1", arguments={})
        openai_client.add_tool_result_to_conversation(conv, tc, '{"nodes": []}')

        assert len(conv) == 1
        assert conv[0]["type"] == "function_call_output"
        assert conv[0]["call_id"] == "call_1"
        assert conv[0]["output"] == '{"nodes": []}'

    @pytest.mark.asyncio
    async def test_save_usage(self, openai_client: OpenAiClient):
        raw = """
        {
            "output": [],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150
            }
        }
        """
        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        openai_client.db_manager.session.return_value = mock_ctx

        await openai_client.save_usage(raw, 123)

        assert mock_session.add.called
        usage = mock_session.add.call_args[0][0]
        assert isinstance(usage, LlmTokenUsage)
        assert usage.telegram_id == 123
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_get_provider_name(self, openai_client: OpenAiClient):
        assert openai_client.get_provider_name() == "OpenAI"
