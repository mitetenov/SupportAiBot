"""Tests for OpenRouter and Z.AI logging across TRACE, INFO, and ERROR levels.

Verifies:
- Exact-value credential redaction via register_settings_secrets for synthetic keys without 'sk-' prefixes.
- TRACE: requests, responses, tools, reasoning, retries, and fallback with redacted headers and keys.
- INFO: provider/model/effort, safe operational events, no payloads or private context.
- ERROR: safe type/status/code metadata without user context, tool arguments, or provider bodies.
- Absence of expensive TRACE serialization on INFO/ERROR levels.
- Immutability of payloads and reasoning signatures during redaction.
- Injected client with hooks vs lazy client without duplicate hooks or duplicate log records.
"""

from __future__ import annotations

import copy
import io
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.llm.base import (
    FaqContext,
    LlmProcessingException,
)
from app.llm.fallback import LlmFallbackClient
from app.llm.mcp_client import McpTool
from app.llm.mcp_router import McpRouter
from app.llm.openrouter import OpenRouterClient
from app.llm.zai import ZaiClient
from app.logging_config import setup_logging
from app.logging_context import clear_context
from app.logging_http import create_logging_hooks
from app.logging_redaction import (
    clear_registered_secrets,
    redact_data,
    register_settings_secrets,
)
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService

# Unique synthetic keys without standard 'sk-' or 'gsk_' prefixes
SYNTHETIC_OPENROUTER_KEY = "SECRET_OPENROUTER_CUSTOM_KEY_XYZ_99999"
SYNTHETIC_ZAI_KEY = "SECRET_ZAI_CUSTOM_KEY_ABC_88888"


@pytest.fixture(autouse=True)
def _reset_logging_and_secrets() -> Iterator[None]:
    clear_context()
    clear_registered_secrets()
    yield
    clear_context()
    clear_registered_secrets()
    setup_logging(level="INFO")


@pytest.fixture
def mock_mcp_router() -> MagicMock:
    router = MagicMock(spec=McpRouter)
    router.list_tools.return_value = [
        McpTool(
            name="nodes_list",
            description="List active VPN server nodes",
            input_schema={
                "type": "object",
                "properties": {"region": {"type": "string"}},
                "required": ["region"],
            },
        )
    ]
    router.call_tool = AsyncMock(return_value="Node 1 (Frankfurt) is online")
    return router


@pytest.fixture
def mock_history_service() -> MagicMock:
    history = MagicMock(spec=ChatHistoryService)
    history.get_history = AsyncMock(return_value=[])
    history.add_user_message = AsyncMock()
    history.add_assistant_message = AsyncMock()
    history.add_rejected_faq_questions = MagicMock()
    history.clear_rejected_faqs_if_new_topic = MagicMock()
    history.get_last_user_message = MagicMock(return_value=None)
    history.get_rejected_faq_questions = MagicMock(return_value=[])
    return history


@pytest.fixture
def mock_faq_service() -> MagicMock:
    faq = MagicMock(spec=FaqEmbeddingService)
    faq.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)
    return faq


def _make_openrouter_settings(
    valid_settings_dict: dict[str, Any],
    model: str = "z-ai/glm-4.7",
    effort: str = "none",
) -> Settings:
    data = dict(valid_settings_dict)
    data["llm_provider"] = "openrouter"
    data["openrouter_api_key"] = SYNTHETIC_OPENROUTER_KEY
    data["openrouter_model"] = model
    data["reasoning_effort"] = effort
    return Settings(**data)


def _make_zai_settings(
    valid_settings_dict: dict[str, Any],
    model: str = "glm-4.7",
    effort: str = "low",
) -> Settings:
    data = dict(valid_settings_dict)
    data["llm_provider"] = "zai"
    data["zai_api_key"] = SYNTHETIC_ZAI_KEY
    data["zai_model"] = model
    data["reasoning_effort"] = effort
    return Settings(**data)


class TestOpenRouterLogging:
    """TRACE, INFO, and ERROR level logging for OpenRouterClient."""

    @pytest.mark.asyncio
    async def test_trace_logging_full_flow_redacts_credentials(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        settings = _make_openrouter_settings(
            valid_settings_dict, model="z-ai/glm-4.7", effort="none"
        )
        register_settings_secrets(settings)

        user_secret_query = "SUPER_SECRET_USER_ISSUE_101"
        faq_secret_text = "CONFIDENTIAL_FAQ_INTERNAL_INSTRUCTIONS_202"
        mock_faq_service.build_faq_context = AsyncMock(
            return_value=FaqContext(
                text=faq_secret_text,
                results=[],
                max_similarity=0.0,
                best_question=None,
            )
        )

        # Multi-turn response: 1st turn calls tool with reasoning, 2nd turn returns text
        request_count = 0

        def handle_request(req: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning": "TRACE_REASONING_STEP_ONE",
                                    "tool_calls": [
                                        {
                                            "id": "call_nodes_1",
                                            "type": "function",
                                            "function": {
                                                "name": "nodes_list",
                                                "arguments": '{"region": "eu-central"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Серверы в регионе eu-central доступны.",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
                },
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        reply = await client.do_chat(user_secret_query, 12345)
        assert reply.text == "Серверы в регионе eu-central доступны."

        logs = stream.getvalue()

        # 1. TRACE diagnostics present
        assert "OpenRouter API request (model=z-ai/glm-4.7):" in logs
        assert "OpenRouter request (1 tools available)" in logs
        assert "OpenRouter API parsed response (model=z-ai/glm-4.7):" in logs
        assert "TRACE_REASONING_STEP_ONE" in logs
        assert "call_nodes_1" in logs

        # 2. Credential sanitization in TRACE
        assert SYNTHETIC_OPENROUTER_KEY not in logs
        assert "[REDACTED]" in logs
        assert f"Bearer {SYNTHETIC_OPENROUTER_KEY}" not in logs

    @pytest.mark.asyncio
    async def test_trace_logging_retries_and_fallback(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        primary_settings = _make_openrouter_settings(valid_settings_dict, model="z-ai/glm-4.7")
        backup_settings = _make_zai_settings(valid_settings_dict, model="glm-4.7")
        register_settings_secrets(primary_settings)
        register_settings_secrets(backup_settings)

        primary_attempts = 0

        def handle_primary(req: httpx.Request) -> httpx.Response:
            nonlocal primary_attempts
            primary_attempts += 1
            return httpx.Response(500, json={"error": {"message": "Internal gateway failure"}})

        def handle_backup(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Backup answer."}}]},
            )

        primary_client = OpenRouterClient(
            settings=primary_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_primary)),
        )
        backup_client = ZaiClient(
            settings=backup_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_backup)),
        )

        fallback_client = LlmFallbackClient([primary_client, backup_client])
        reply = await fallback_client.chat("test message", 12345)
        assert reply.text == "Backup answer."

        logs = stream.getvalue()

        # Retry and fallback diagnostics in TRACE
        assert "attempt 1" in logs or "attempt=1" in logs
        assert "attempt 2" in logs or "attempt=2" in logs
        assert "attempt 3" in logs or "attempt=3" in logs
        assert "LlmFallbackClient: provider OpenRouter failed" in logs
        assert "falling back to next provider" in logs

        # Exact-value redaction confirmed
        assert SYNTHETIC_OPENROUTER_KEY not in logs
        assert SYNTHETIC_ZAI_KEY not in logs

    @pytest.mark.asyncio
    async def test_info_logging_does_not_leak_payloads_or_private_context(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        settings = _make_openrouter_settings(
            valid_settings_dict, model="z-ai/glm-4.7", effort="none"
        )
        register_settings_secrets(settings)

        secret_user_prompt = "SENSITIVE_USER_CREDENTIAL_INQUIRY_333"
        secret_faq_context = "SECRET_INTERNAL_RUNBOOK_444"
        secret_reply_text = "SECRET_RESPONSE_PAYLOAD_555"
        mock_faq_service.build_faq_context = AsyncMock(
            return_value=FaqContext(
                text=secret_faq_context,
                results=[],
                max_similarity=0.0,
                best_question=None,
            )
        )

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": secret_reply_text}}]
                },
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        reply = await client.do_chat(secret_user_prompt, 12345)
        assert reply.text == secret_reply_text

        logs = stream.getvalue()

        # 1. Model and effort emitted on INFO
        assert "Selected LLM: provider=OpenRouter, model=z-ai/glm-4.7" in logs
        assert "configured_effort=none, effective_effort=none" in logs

        # 2. No payload or context leaks
        assert secret_user_prompt not in logs
        assert secret_faq_context not in logs
        assert secret_reply_text not in logs
        assert SYNTHETIC_OPENROUTER_KEY not in logs
        assert "HTTP request:" not in logs
        assert "HTTP response:" not in logs

    @pytest.mark.asyncio
    async def test_error_logging_safe_metadata_without_payload_leaks(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        settings = _make_openrouter_settings(
            valid_settings_dict, model="z-ai/glm-4.7", effort="none"
        )
        register_settings_secrets(settings)

        secret_prompt = "LEAK_PROMPT_CHECK_777"
        secret_error_body = "SENSITIVE_PROVIDER_DIAGNOSTIC_BODY_999"

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": f"Unauthorized with key {SYNTHETIC_OPENROUTER_KEY}: {secret_error_body}"
                    }
                },
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.do_chat(secret_prompt, 12345)

        assert exc_info.value.status_code == 401
        assert exc_info.value.fallback_eligible is True

        logs = stream.getvalue()

        # 1. Safe error metadata emitted
        assert "OpenRouter API error (model=z-ai/glm-4.7, status=401)" in logs

        # 2. No payload, secret, or provider body leaks in logs or exception str
        assert SYNTHETIC_OPENROUTER_KEY not in logs
        assert SYNTHETIC_OPENROUTER_KEY not in str(exc_info.value)
        assert secret_prompt not in logs
        assert secret_error_body not in logs
        assert secret_error_body not in str(exc_info.value)


class TestZaiLogging:
    """TRACE, INFO, and ERROR level logging for ZaiClient."""

    @pytest.mark.asyncio
    async def test_trace_logging_full_flow_redacts_credentials(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        settings = _make_zai_settings(valid_settings_dict, model="glm-4.7", effort="low")
        register_settings_secrets(settings)

        user_secret = "USER_SECRET_TOKEN_IN_PROMPT_54321"

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Ответ от Z.AI",
                                "reasoning_content": "ZAI_TRACE_REASONING_FLOW_STEP",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        reply = await client.do_chat(user_secret, 67890)
        assert reply.text == "Ответ от Z.AI"

        logs = stream.getvalue()

        # 1. TRACE diagnostics present
        assert "Z.AI API request (model=glm-4.7):" in logs
        assert "Z.AI API parsed response (model=glm-4.7):" in logs
        assert "ZAI_TRACE_REASONING_FLOW_STEP" in logs

        # 2. Redaction verified
        assert SYNTHETIC_ZAI_KEY not in logs
        assert "[REDACTED]" in logs
        assert f"Bearer {SYNTHETIC_ZAI_KEY}" not in logs

    @pytest.mark.asyncio
    async def test_info_logging_does_not_leak_payloads(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        settings = _make_zai_settings(valid_settings_dict, model="glm-4.7", effort="high")
        register_settings_secrets(settings)

        secret_user_prompt = "TOP_SECRET_ZAI_USER_QUESTION_888"
        secret_reply_text = "TOP_SECRET_ZAI_REPLY_777"

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": secret_reply_text}}]
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        reply = await client.do_chat(secret_user_prompt, 12345)
        assert reply.text == secret_reply_text

        logs = stream.getvalue()

        # 1. Model selection emitted on INFO
        assert "Selected LLM: provider=Z.AI, model=glm-4.7" in logs
        assert "configured_effort=high, effective_effort=enabled" in logs

        # 2. No payload leaks
        assert secret_user_prompt not in logs
        assert secret_reply_text not in logs
        assert SYNTHETIC_ZAI_KEY not in logs
        assert "HTTP request:" not in logs

    @pytest.mark.asyncio
    async def test_error_logging_zai_business_error_code(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        settings = _make_zai_settings(valid_settings_dict, model="glm-4.7", effort="low")
        register_settings_secrets(settings)

        secret_body = "SENSITIVE_ZAI_BALANCE_EXHAUSTION_DETAILS"

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 1113, "msg": f"Account balance exhausted: {secret_body}"},
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.do_chat("Проверка баланса", 12345)

        assert exc_info.value.fallback_eligible is True
        assert "code=1113" in str(exc_info.value)
        assert secret_body not in str(exc_info.value)

        logs = stream.getvalue()
        assert SYNTHETIC_ZAI_KEY not in logs
        assert secret_body not in logs

    @pytest.mark.asyncio
    async def test_trace_logging_zai_retries_and_fallback(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        primary_settings = _make_zai_settings(valid_settings_dict, model="glm-4.7", effort="low")
        backup_settings = _make_openrouter_settings(valid_settings_dict, model="z-ai/glm-4.7")
        register_settings_secrets(primary_settings)
        register_settings_secrets(backup_settings)

        def handle_primary(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "Service unavailable"}})

        def handle_backup(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Backup answer from OpenRouter.",
                            }
                        }
                    ]
                },
            )

        primary_client = ZaiClient(
            settings=primary_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_primary)),
        )
        backup_client = OpenRouterClient(
            settings=backup_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_backup)),
        )

        fallback_client = LlmFallbackClient([primary_client, backup_client])
        reply = await fallback_client.chat("Need help with wireguard", 12345)
        assert reply.text == "Backup answer from OpenRouter."

        logs = stream.getvalue()

        # Retry and fallback diagnostics
        assert "attempt 1" in logs or "attempt=1" in logs
        assert "attempt 2" in logs or "attempt=2" in logs
        assert "attempt 3" in logs or "attempt=3" in logs
        assert "LlmFallbackClient: provider Z.AI failed" in logs
        assert "falling back to next provider" in logs
        assert "LLM fallback transition: from Z.AI to OpenRouter" in logs

        # Exact-value redaction confirmed
        assert SYNTHETIC_ZAI_KEY not in logs
        assert SYNTHETIC_OPENROUTER_KEY not in logs

    def test_info_logging_unsupported_reasoning_effort_models(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        # OpenRouter unknown model
        or_settings = _make_openrouter_settings(
            valid_settings_dict, model="meta-llama/llama-3.3-70b-instruct", effort="medium"
        )
        or_client = OpenRouterClient(
            settings=or_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )
        assert or_client.get_effective_reasoning_effort() == "unsupported/ignored"

        # Z.AI unknown model
        zai_settings = _make_zai_settings(valid_settings_dict, model="glm-4-air", effort="high")
        zai_client = ZaiClient(
            settings=zai_settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )
        assert zai_client.get_effective_reasoning_effort() == "unsupported/ignored"

        logs = stream.getvalue()
        assert (
            "Selected LLM: provider=OpenRouter, model=meta-llama/llama-3.3-70b-instruct, configured_effort=medium, effective_effort=unsupported/ignored"
            in logs
        )
        assert (
            "Selected LLM: provider=Z.AI, model=glm-4-air, configured_effort=high, effective_effort=unsupported/ignored"
            in logs
        )


class TestLoggingPerformanceAndImmutability:
    """Ensure logging redaction is efficient and does not mutate objects."""

    @pytest.mark.asyncio
    async def test_no_expensive_trace_serialization_on_info_level(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        settings = _make_openrouter_settings(valid_settings_dict)
        register_settings_secrets(settings)

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Ok"}}]},
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )

        with patch("app.llm.chat_completions.safe_serialize") as mock_serialize:
            await client.do_chat("Hello", 12345)
            # safe_serialize must NOT be called when logger level is INFO
            mock_serialize.assert_not_called()

    def test_redaction_does_not_mutate_original_payload_or_reasoning(self) -> None:
        original_body: dict[str, Any] = {
            "model": "z-ai/glm-4.7",
            "messages": [
                {"role": "user", "content": "Help with VPN"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "Keep this signature intact",
                    "reasoning_details": [{"type": "thought", "signature": "sig_xyz_123"}],
                },
            ],
            "api_key": SYNTHETIC_OPENROUTER_KEY,
        }
        original_copy = copy.deepcopy(original_body)

        redacted = redact_data(original_body)

        # Original structure is unchanged
        assert original_body == original_copy
        assert original_body["api_key"] == SYNTHETIC_OPENROUTER_KEY
        assert original_body["messages"][1]["reasoning"] == "Keep this signature intact"
        assert original_body["messages"][1]["reasoning_details"][0]["signature"] == "sig_xyz_123"

        # Redacted copy has secret removed
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["messages"][1]["reasoning"] == "Keep this signature intact"


class TestHttpClientLoggingHooksLifecycle:
    """Test hook attachment on injected vs lazy client without duplication."""

    @pytest.mark.asyncio
    async def test_injected_client_with_hooks_does_not_duplicate_transport_logs(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        settings = _make_openrouter_settings(valid_settings_dict)

        def handle_request(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "Injected response"}}]
                },
            )

        # Injected client created with production logging hooks
        injected_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request),
            event_hooks=create_logging_hooks(),
        )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=injected_client,
        )

        assert client._own_client is False
        assert client.http_client is injected_client

        reply = await client.do_chat("test query", 12345)
        assert reply.text == "Injected response"

        logs = stream.getvalue()
        # Exactly one HTTP request and response record (no hook duplication)
        assert logs.count("HTTP request: POST") == 1
        assert logs.count("HTTP response: POST") == 1

        await client.close()
        # Injected client not closed by client.close()
        assert not injected_client.is_closed
        await injected_client.aclose()

    @pytest.mark.asyncio
    async def test_lazy_client_initialization_attaches_hooks_cleanly(
        self,
        valid_settings_dict: dict[str, Any],
        mock_mcp_router: MagicMock,
        mock_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        settings = _make_zai_settings(valid_settings_dict)

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=None,
        )

        assert client._http_client is None
        assert client._own_client is False

        # Access property lazily initializes client with hooks
        http_client = client.http_client
        assert client._http_client is not None
        assert client._own_client is True
        assert "request" in http_client.event_hooks
        assert "response" in http_client.event_hooks

        await client.close()
        assert http_client.is_closed
