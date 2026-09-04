"""Tests for cross-provider LLM fallback orchestration."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from app.config import LlmProviderTarget, Settings
from app.llm import create_llm_client
from app.llm.base import LlmProcessingException, LlmReply
from app.llm.fallback import LlmFallbackClient, LlmFallbackExhaustedError
from app.llm.mcp_router import McpRouter
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqContext
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager
from app.storage.models import LlmTokenUsage


class _FakeClient:
    def __init__(self, name: str, result: LlmReply | Exception) -> None:
        self.name = name
        self.result = result
        self.calls: list[tuple[str, int, object]] = []
        self.chat_history_service = SimpleNamespace(
            add_user_message=AsyncMock(),
            add_assistant_message=AsyncMock(),
            add_rejected_faq_questions=MagicMock(),
        )

    async def prepare_turn(self, user_message: str, telegram_user_id: int) -> object:
        self.prepared = (user_message, telegram_user_id)
        return SimpleNamespace(replay_completed_tool_results=False, completed_tool_results={})

    async def do_chat(
        self,
        user_message: str,
        telegram_user_id: int,
        base64_image: str | None = None,
        mime_type: str | None = None,
        turn_state: object | None = None,
    ) -> LlmReply:
        self.calls.append((user_message, telegram_user_id, turn_state))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get_provider_name(self) -> str:
        return self.name

    def supports_images(self) -> bool:
        return True


def _fallback_settings(**overrides: object) -> Settings:
    """Return a two-provider configuration whose endpoints are MockTransport-only."""
    values: dict[str, object] = {
        "telegram_bot_token": "test-token",
        "telegram_support_group_chat_id": -1001234567890,
        "llm_provider": "deepseek",
        "llm_fallback_chain": "groq:backup-model",
        "embedding_provider": "gemini",
        "gemini_api_key": "gemini-test-key",
        "deepseek_api_key": "primary-test-key",
        "deepseek_model": "primary-model",
        "deepseek_base_url": "https://primary.invalid/v1",
        "groq_api_key": "backup-test-key",
        "groq_base_url": "https://backup.invalid/openai/v1",
        "remnawave_mcp_url": "http://localhost:3100",
    }
    values.update(overrides)
    return Settings(**values)


def _concrete_dependencies() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Create stateful dependencies for real provider clients without infrastructure."""
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.list_tools.return_value = []
    mcp_router.call_tool = AsyncMock(return_value="tool result")

    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_rejected_faq_questions.return_value = set()
    chat_history_service.get_last_user_message.return_value = None
    chat_history_service.get_history = AsyncMock(
        return_value=[{"role": "user", "content": "earlier"}]
    )
    chat_history_service.add_user_message = AsyncMock()
    chat_history_service.add_assistant_message = AsyncMock()

    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)
    faq_embedding_service.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)

    token_session = MagicMock()
    token_context = AsyncMock()
    token_context.__aenter__.return_value = token_session
    token_context.__aexit__.return_value = False
    db_manager = MagicMock(spec=DatabaseSessionManager)
    db_manager.session.return_value = token_context
    return mcp_router, chat_history_service, faq_embedding_service, db_manager, token_session


class TestLlmFallbackClient:
    @pytest.mark.asyncio
    async def test_returns_primary_reply_without_using_fallback(self) -> None:
        primary = _FakeClient("Primary", LlmReply(text="served by primary"))
        secondary = _FakeClient("Secondary", LlmReply(text="must not be used"))

        reply = await LlmFallbackClient([primary, secondary]).chat("Need help", 42)

        assert reply.text == "served by primary"
        assert len(primary.calls) == 1
        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_falls_back_after_rate_limit_without_persisting_failed_turn(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("rate limited", status_code=429))
        secondary = _FakeClient("Secondary", LlmReply(text="served by backup"))
        client = LlmFallbackClient([primary, secondary])

        reply = await client.chat("Need help", 42)

        assert reply.text == "served by backup"
        assert primary.prepared == ("Need help", 42)
        assert len(primary.calls) == 1
        assert len(secondary.calls) == 1
        assert secondary.calls[0][2] is primary.calls[0][2]
        primary.chat_history_service.add_user_message.assert_not_awaited()
        secondary.chat_history_service.add_user_message.assert_awaited_once_with(42, "Need help")
        secondary.chat_history_service.add_assistant_message.assert_awaited_once_with(
            42, "served by backup"
        )

    @pytest.mark.asyncio
    async def test_does_not_fallback_after_non_retryable_request_error(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("bad request", status_code=400))
        secondary = _FakeClient("Secondary", LlmReply(text="must not be used"))
        client = LlmFallbackClient([primary, secondary])

        with pytest.raises(LlmProcessingException, match="bad request"):
            await client.chat("Need help", 42)

        assert len(primary.calls) == 1
        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_falls_back_after_recognized_balance_exhaustion(self) -> None:
        primary = _FakeClient(
            "Primary",
            LlmProcessingException("request rejected", fallback_eligible=True),
        )
        secondary = _FakeClient("Secondary", LlmReply(text="served by backup"))
        client = LlmFallbackClient([primary, secondary])

        reply = await client.chat("Need help", 42)

        assert reply.text == "served by backup"
        assert len(secondary.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 402, 403, 408, 429])
    async def test_falls_back_after_each_configured_provider_status(self, status_code: int) -> None:
        primary = _FakeClient(
            "Primary", LlmProcessingException("provider unavailable", status_code=status_code)
        )
        secondary = _FakeClient("Secondary", LlmReply(text="served by backup"))

        reply = await LlmFallbackClient([primary, secondary]).chat("Need help", 42)

        assert reply.text == "served by backup"
        assert len(primary.calls) == 1
        assert len(secondary.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("timed out"),
            httpx.ConnectError("network unavailable"),
        ],
        ids=["timeout", "transport-error"],
    )
    async def test_falls_back_after_timeout_or_transport_error(self, error: Exception) -> None:
        primary = _FakeClient("Primary", error)
        secondary = _FakeClient("Secondary", LlmReply(text="served by backup"))

        reply = await LlmFallbackClient([primary, secondary]).chat("Need help", 42)

        assert reply.text == "served by backup"
        assert len(primary.calls) == 1
        assert len(secondary.calls) == 1

    @pytest.mark.asyncio
    async def test_does_not_fallback_after_an_invalid_model_response(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("No content returned"))
        secondary = _FakeClient("Secondary", LlmReply(text="must not be used"))

        with pytest.raises(LlmProcessingException, match="No content returned"):
            await LlmFallbackClient([primary, secondary]).chat("Need help", 42)

        assert len(primary.calls) == 1
        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_returns_safe_domain_error_after_chain_is_exhausted(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("quota exhausted", status_code=429))
        secondary = _FakeClient("Secondary", LlmProcessingException("denied", status_code=403))
        client = LlmFallbackClient([primary, secondary])

        with pytest.raises(LlmFallbackExhaustedError) as exc_info:
            await client.chat("Need help", 42)

        assert (
            exc_info.value.user_friendly_message == "Сервис временно недоступен. Попробуйте позже."
        )
        assert "quota exhausted" not in str(exc_info.value)
        assert "denied" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_does_not_fail_over_after_a_completed_tool_call(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("timeout", status_code=408))
        secondary = _FakeClient("Secondary", LlmReply(text="must not be used"))

        async def fail_after_tool(
            user_message: str,
            telegram_user_id: int,
            base64_image: str | None = None,
            mime_type: str | None = None,
            turn_state: object | None = None,
        ) -> LlmReply:
            assert turn_state is not None
            turn_state.completed_tool_results["hwid_device_delete:{}"] = "deleted"
            raise LlmProcessingException("timeout", status_code=408)

        primary.do_chat = fail_after_tool  # type: ignore[method-assign]
        client = LlmFallbackClient([primary, secondary])

        with pytest.raises(LlmFallbackExhaustedError):
            await client.chat("Need help", 42)

        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_real_clients_use_mock_transport_and_preserve_turn_state_on_failover(
        self,
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "primary.invalid":
                return httpx.Response(401, json={"error": "primary secret must not escape"})
            if request.url.host == "backup.invalid":
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "served by Groq"}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    },
                )
            raise AssertionError(f"unexpected network target: {request.url!s}")

        mcp_router, history, faq_service, db_manager, token_session = _concrete_dependencies()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = create_llm_client(
                _fallback_settings(),
                mcp_router,
                history,
                faq_service,
                db_manager,
                http_client,
            )

            reply = await client.chat("Need help", 42)

        assert reply.text == "served by Groq"
        assert [request.url.host for request in requests] == ["primary.invalid", "backup.invalid"]
        assert [json.loads(request.content)["model"] for request in requests] == [
            "primary-model",
            "backup-model",
        ]
        assert (
            json.loads(requests[0].content)["messages"]
            == json.loads(requests[1].content)["messages"]
        )
        history.clear_rejected_faqs_if_new_topic.assert_called_once_with(42, "Need help")
        faq_service.build_faq_context.assert_awaited_once()
        history.add_user_message.assert_awaited_once_with(42, "Need help")
        history.add_assistant_message.assert_awaited_once_with(42, "served by Groq")
        mcp_router.call_tool.assert_not_awaited()
        assert token_session.add.call_count == 1
        usage = token_session.add.call_args.args[0]
        assert isinstance(usage, LlmTokenUsage)
        assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (11, 7, 18)

    @pytest.mark.asyncio
    async def test_real_tool_call_is_not_replayed_after_provider_failure(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "primary.invalid" and len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "function": {
                                                "name": "nodes_list",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            if request.url.host == "primary.invalid":
                return httpx.Response(401, json={"error": "provider failure"})
            raise AssertionError(
                f"fallback must not make a request after a tool call: {request.url!s}"
            )

        mcp_router, history, faq_service, db_manager, _ = _concrete_dependencies()
        mcp_router.list_tools.return_value = [
            SimpleNamespace(name="nodes_list", description="List nodes", input_schema={})
        ]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = create_llm_client(
                _fallback_settings(),
                mcp_router,
                history,
                faq_service,
                db_manager,
                http_client,
            )

            with pytest.raises(LlmFallbackExhaustedError):
                await client.chat("Need help", 42)

        assert [request.url.host for request in requests] == ["primary.invalid", "primary.invalid"]
        mcp_router.call_tool.assert_awaited_once_with("nodes_list", {}, 42)
        history.add_user_message.assert_not_awaited()
        history.add_assistant_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhaustion_logs_and_exceptions_never_include_provider_response_secrets(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "provider-response-secret"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": secret})

        mcp_router, history, faq_service, db_manager, _ = _concrete_dependencies()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = create_llm_client(
                _fallback_settings(),
                mcp_router,
                history,
                faq_service,
                db_manager,
                http_client,
            )

            with (
                caplog.at_level(logging.WARNING),
                pytest.raises(LlmFallbackExhaustedError) as exc_info,
            ):
                await client.chat("Need help", 42)

        assert secret not in caplog.text
        assert secret not in str(exc_info.value)
        history.add_user_message.assert_not_awaited()
        history.add_assistant_message.assert_not_awaited()


class TestFallbackConfiguration:
    def test_parses_ordered_provider_model_targets(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict.update(
            {
                "llm_fallback_chain": "groq:llama-3.3-70b-versatile, gemini:gemini-2.5-flash",
                "groq_api_key": "groq-test-key",
            }
        )

        settings = Settings(**valid_settings_dict)

        assert settings.llm_fallback_chain == (
            LlmProviderTarget(provider="groq", model="llama-3.3-70b-versatile"),
            LlmProviderTarget(provider="gemini", model="gemini-2.5-flash"),
        )

    def test_rejects_unknown_fallback_provider(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict["llm_fallback_chain"] = "unsupported:model"

        with pytest.raises(ValidationError, match="LLM_FALLBACK_CHAIN"):
            Settings(**valid_settings_dict)
