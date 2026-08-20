"""Tests for GeminiClient (Gemini REST API, schema sanitization, thought signature, vision)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.llm.base import LlmProcessingException
from app.llm.gemini import GeminiClient, sanitize_schema_params
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
        gemini_api_key="gemini-test-key",
        gemini_model="gemini-2.5-flash",
        gemini_base_url="http://localhost:9999",
        remnawave_mcp_url="http://localhost:3100",
    )


@pytest.fixture
def gemini_client(settings: Settings):
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.list_tools.return_value = []
    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_history = AsyncMock(return_value=[])
    chat_history_service.to_gemini_contents = AsyncMock(return_value=[])
    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)
    db_manager = MagicMock(spec=DatabaseSessionManager)

    client = GeminiClient(
        settings=settings,
        mcp_router=mcp_router,
        chat_history_service=chat_history_service,
        faq_embedding_service=faq_embedding_service,
        db_manager=db_manager,
    )
    return client


class TestGeminiClient:
    def test_supports_images(self, gemini_client: GeminiClient):
        assert gemini_client.supports_images() is True

    def test_build_request_body_includes_system_prompt_and_tool_config(self, gemini_client: GeminiClient):
        body = gemini_client.build_request_body([])
        system_instruction = body.get("system_instruction")
        assert system_instruction is not None
        system_text = system_instruction["parts"][0]["text"]
        assert "Ты — техподдержка VPN-сервиса" in system_text
        assert "Telegram ID: 12345" not in system_text

        assert "tool_config" in body
        assert body["tool_config"]["function_calling_config"]["mode"] == "AUTO"

    def test_build_initial_conversation_for_text(self, gemini_client: GeminiClient):
        conv = gemini_client.build_initial_conversation("Hello", 123, "FAQ content", None, None)
        assert len(conv) >= 3
        # First turn: user with dynamic context
        assert conv[0]["role"] == "user"
        first_text = conv[0]["parts"][0]["text"]
        assert "Telegram ID: 123" in first_text
        assert "FAQ content" in first_text

        # Second turn: model acknowledgement
        assert conv[1]["role"] == "model"

        # Last turn: user message
        assert conv[-1]["role"] == "user"
        assert conv[-1]["parts"][0]["text"] == "Hello"

    def test_build_initial_conversation_for_image(self, gemini_client: GeminiClient):
        conv = gemini_client.build_initial_conversation("Describe", 123, "FAQ", "base64data", "image/png")
        assert conv[-1]["role"] == "user"
        parts = conv[-1]["parts"]
        assert len(parts) == 2
        assert parts[0]["text"] == "Describe"
        assert parts[1]["inline_data"]["data"] == "base64data"
        assert parts[1]["inline_data"]["mime_type"] == "image/png"

    def test_parse_response_with_text_and_thought_signature(self, gemini_client: GeminiClient):
        raw_response = """
        {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Let me check your devices."},
                        {
                            "functionCall": {
                                "name": "hwid_devices_list",
                                "args": {"uuid": "abc-123"},
                                "thought_signature": "sig_abc123xyz"
                            }
                        }
                    ]
                }
            }],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "totalTokenCount": 150
            }
        }
        """
        response = gemini_client.parse_response(raw_response)
        assert response.text == "Let me check your devices."
        assert len(response.tool_calls) == 1
        tc = response.tool_calls[0]
        assert tc.name == "hwid_devices_list"
        assert tc.arguments == {"uuid": "abc-123"}
        assert tc.thought_signature == "sig_abc123xyz"

    def test_preserve_thought_signature_in_conversation(self, gemini_client: GeminiClient):
        raw_response = """
        {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "hwid_devices_list",
                                "args": {"uuid": "abc-123"},
                                "thought_signature": "sig_preserve_me"
                            }
                        }
                    ]
                }
            }]
        }
        """
        response = gemini_client.parse_response(raw_response)
        conversation = []
        gemini_client.add_tool_calls_to_conversation(conversation, response)

        assert len(conversation) == 1
        assert conversation[0]["role"] == "model"
        fc = conversation[0]["parts"][0]["functionCall"]
        assert fc["name"] == "hwid_devices_list"
        assert fc["thought_signature"] == "sig_preserve_me"

        # Now add tool result
        tc = response.tool_calls[0]
        gemini_client.add_tool_result_to_conversation(conversation, tc, '{"devices": []}')

        assert len(conversation) == 2
        func_msg = conversation[1]
        assert func_msg["role"] == "function"
        fn_resp = func_msg["parts"][0]["functionResponse"]
        assert fn_resp["name"] == "hwid_devices_list"
        assert fn_resp["thought_signature"] == "sig_preserve_me"
        assert fn_resp["response"] == {"devices": []}

    def test_schema_sanitization(self):
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "propertyNames": {"pattern": "^[a-z]+$"},
            "type": "object",
            "properties": {
                "kind": {"const": "device"},
                "mode": {"any_of": [{"type": "string"}, {"type": "integer"}]},
                "nested": {
                    "type": "object",
                    "properties": {
                        "extra": {"additionalProperties": False, "type": "string"}
                    }
                }
            }
        }
        sanitized = sanitize_schema_params(schema)
        assert "$schema" not in sanitized
        assert "additionalProperties" not in sanitized
        assert "propertyNames" not in sanitized
        assert sanitized["properties"]["kind"] == {"enum": ["device"]}
        assert "anyOf" in sanitized["properties"]["mode"]
        assert "additionalProperties" not in sanitized["properties"]["nested"]["properties"]["extra"]

    @pytest.mark.asyncio
    async def test_save_usage(self, gemini_client: GeminiClient):
        raw_response = """
        {
            "candidates": [],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "totalTokenCount": 150
            }
        }
        """
        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        gemini_client.db_manager.session.return_value = mock_ctx

        await gemini_client.save_usage(raw_response, 123)

        assert mock_session.add.called
        added_usage = mock_session.add.call_args[0][0]
        assert isinstance(added_usage, LlmTokenUsage)
        assert added_usage.telegram_id == 123
        assert added_usage.prompt_tokens == 100
        assert added_usage.completion_tokens == 50
        assert added_usage.total_tokens == 150

    def test_parse_response_empty_candidates_throws(self, gemini_client: GeminiClient):
        raw = '{"promptFeedback": {"blockReason": "SAFETY"}}'
        with pytest.raises(LlmProcessingException) as exc_info:
            gemini_client.parse_response(raw)
        assert "Empty candidates" in str(exc_info.value)
