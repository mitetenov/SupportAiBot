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
    @pytest.mark.parametrize("status_code", [401, 402, 403, 408, 413, 429])
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
    async def test_fails_over_after_a_completed_tool_call(self) -> None:
        primary = _FakeClient("Primary", LlmProcessingException("timeout", status_code=408))
        secondary = _FakeClient("Secondary", LlmReply(text="served by backup"))

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

        reply = await client.chat("Need help", 42)

        assert reply.text == "served by backup"
        assert len(secondary.calls) == 1
        assert secondary.calls[0][2].completed_tool_results == {"hwid_device_delete:{}": "deleted"}

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
    @pytest.mark.parametrize("tool_name", ["nodes_list", "hwid_device_delete"])
    @pytest.mark.parametrize("repeat_call", [False, True])
    async def test_completed_tools_survive_failover_without_reexecution(
        self, tool_name: str, repeat_call: bool
    ) -> None:
        requests: list[httpx.Request] = []
        backup_requests: list[dict] = []
        args = {"userId": "user-42", "hwid": "device-1"}

        def tool_payload(call_id: str, arguments: dict) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "primary.invalid":
                if len(requests) == 1:
                    return httpx.Response(200, json=tool_payload("primary-call", args))
                return httpx.Response(401, json={"error": "provider failure"})
            body = json.loads(request.content)
            backup_requests.append(body)
            if repeat_call and len(backup_requests) == 1:
                # Call IDs and dictionary insertion order are provider-specific.
                return httpx.Response(
                    200, json=tool_payload("backup-call", dict(reversed(list(args.items()))))
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

        router, history, faq, db, token_session = _concrete_dependencies()
        router.list_tools.return_value = [
            SimpleNamespace(name=tool_name, description="Tool", input_schema={})
        ]
        router.call_tool.return_value = '{"status":"done"}'
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = create_llm_client(_fallback_settings(), router, history, faq, db, http)
            reply = await client.chat("Need help", 42)

        assert reply.text == "done"
        router.call_tool.assert_awaited_once_with(tool_name, args, 42)
        summary = backup_requests[0]["messages"][-1]["content"]
        records = json.loads(summary.split("\n", 1)[1])
        assert records == [{"tool": tool_name, "arguments": args, "result": '{"status":"done"}'}]
        if repeat_call:
            result = backup_requests[1]["messages"][-1]
            assert result == {
                "role": "tool",
                "tool_call_id": "backup-call",
                "content": '{"status":"done"}',
            }
        assert token_session.add.call_count == (2 if repeat_call else 1)
        history.add_user_message.assert_awaited_once_with(42, "Need help")
        history.add_assistant_message.assert_awaited_once_with(42, "done")
        faq.build_faq_context.assert_awaited_once()

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


@pytest.mark.asyncio
@pytest.mark.parametrize("backup", ["gemini", "openai"])
async def test_cross_provider_transfer_uses_native_text_and_tool_results(backup):
    primary_count = 0
    backup_requests = []
    args = {"hwid": "device-1", "userId": "user-42"}

    def handler(request):
        nonlocal primary_count
        if request.url.host == "primary.invalid":
            primary_count += 1
            if primary_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "primary-call",
                                            "function": {
                                                "name": "hwid_device_delete",
                                                "arguments": json.dumps(args),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(401)
        body = json.loads(request.content)
        backup_requests.append(body)
        if backup == "gemini":
            parts = (
                [
                    {
                        "functionCall": {
                            "name": "hwid_device_delete",
                            "args": args,
                            "id": "backup-call",
                        },
                        "thoughtSignature": "backup-signature",
                    }
                ]
                if len(backup_requests) == 1
                else [{"text": "done"}]
            )
            return httpx.Response(
                200, json={"candidates": [{"content": {"role": "model", "parts": parts}}]}
            )
        output = (
            [
                {
                    "type": "function_call",
                    "name": "hwid_device_delete",
                    "arguments": json.dumps(args),
                    "call_id": "backup-call",
                }
            ]
            if len(backup_requests) == 1
            else [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]
        )
        return httpx.Response(200, json={"output": output})

    router, history, faq, db, _ = _concrete_dependencies()
    history.to_gemini_contents = AsyncMock(return_value=[])
    history.to_openai_messages = AsyncMock(return_value=[])
    router.list_tools.return_value = [
        SimpleNamespace(name="hwid_device_delete", description="Delete device", input_schema={})
    ]
    settings = _fallback_settings(
        llm_fallback_chain=f"{backup}:"
        + ("gemini-3.5-flash" if backup == "gemini" else "gpt-5.6-luna"),
        openai_api_key="sk-test",
        gemini_base_url="https://backup.invalid",
        openai_base_url="https://backup.invalid",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reply = await create_llm_client(settings, router, history, faq, db, http).chat(
            "Delete device", 42
        )
    assert reply.text == "done"
    router.call_tool.assert_awaited_once_with("hwid_device_delete", args, 42)
    if backup == "gemini":
        contents = backup_requests[0]["contents"]
        assert "tool result" in contents[-1]["parts"][0]["text"]
        assert "primary-call" not in json.dumps(contents)
        assert "backup-signature" in json.dumps(backup_requests[1])
        assert backup_requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"] == {
            "output": "tool result"
        }
    else:
        assert "tool result" in backup_requests[0]["input"][-1]["content"]
        assert "primary-call" not in json.dumps(backup_requests[0])
        assert backup_requests[1]["input"][-1] == {
            "type": "function_call_output",
            "call_id": "backup-call",
            "output": "tool result",
        }
    history.add_user_message.assert_awaited_once()
    history.add_assistant_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_timeout_with_unknown_outcome_does_not_reexecute_on_backup():
    from app.llm.base import LlmToolExecutionException

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "function": {"name": "hwid_device_delete", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
        )

    router, history, faq, db, _ = _concrete_dependencies()
    router.call_tool.side_effect = httpx.ReadTimeout("unknown MCP outcome")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = create_llm_client(_fallback_settings(), router, history, faq, db, http)
        with pytest.raises(LlmToolExecutionException):
            await client.chat("Delete device", 42)
    assert len(requests) == 1
    router.call_tool.assert_awaited_once()
    history.add_user_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_targets_share_results_but_next_turn_executes_tools_again():
    counts = {}
    requests = []

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        counts[model] = counts.get(model, 0) + 1
        requests.append(body)
        # Each target first requests a tool, then either fails or answers.
        if counts[model] % 2 == 1:
            tool = "nodes_get" if model == "backup-one" else "nodes_list"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"id": model, "function": {"name": tool, "arguments": "{}"}}
                                ]
                            }
                        }
                    ]
                },
            )
        if model == "backup-two":
            return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
        return httpx.Response(401)

    router, history, faq, db, _ = _concrete_dependencies()
    settings = _fallback_settings(llm_fallback_chain="groq:backup-one,groq:backup-two")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = create_llm_client(settings, router, history, faq, db, http)
        for _ in range(2):
            reply = await client.chat("Need help", 42)
            assert reply.text == "done"
    assert [call.args[0] for call in router.call_tool.await_args_list] == [
        "nodes_list",
        "nodes_get",
    ] * 2
    third_target_context = requests[4]["messages"][-1]["content"]
    records = json.loads(third_target_context.split("\n", 1)[1])
    assert [record["tool"] for record in records] == ["nodes_list", "nodes_get"]
    assert history.add_user_message.await_count == 2
    assert faq.build_faq_context.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("after_tool", [False, True])
@pytest.mark.parametrize("backup_too_large", [False, True])
async def test_groq_413_falls_back_to_luna_without_retrying_oversized_request(
    after_tool, backup_too_large
):
    requests = []
    groq_calls = 0

    def handler(request):
        nonlocal groq_calls
        requests.append(request)
        if request.url.host == "groq.invalid":
            groq_calls += 1
            if after_tool and groq_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "id": "lookup",
                                            "function": {"name": "nodes_list", "arguments": "{}"},
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(413, json={"error": {"message": "request too large"}})
        assert request.url.host == "luna.invalid"
        if backup_too_large:
            return httpx.Response(413, json={"error": {"message": "request too large"}})
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "served by Luna"}],
                    }
                ]
            },
        )

    router, history, faq, db, _ = _concrete_dependencies()
    settings = _fallback_settings(
        llm_provider="groq",
        groq_model="qwen/qwen3.8-27b",
        groq_base_url="https://groq.invalid/openai/v1",
        llm_fallback_chain="openai:gpt-5.6-luna",
        openai_api_key="sk-test",
        openai_base_url="https://luna.invalid/v1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = create_llm_client(settings, router, history, faq, db, http)
        if backup_too_large:
            with pytest.raises(LlmFallbackExhaustedError):
                await client.chat("Need help", 42)
            history.add_user_message.assert_not_awaited()
        else:
            reply = await client.chat("Need help", 42)
            assert reply.text == "served by Luna"
            history.add_user_message.assert_awaited_once_with(42, "Need help")
            history.add_assistant_message.assert_awaited_once_with(42, "served by Luna")

    assert [r.url.host for r in requests] == ["groq.invalid"] * (2 if after_tool else 1) + [
        "luna.invalid"
    ]
    luna_input = json.loads(requests[-1].content)
    assert luna_input["model"] == "gpt-5.6-luna"
    assert {"role": "user", "content": "earlier"} in luna_input["input"]
    assert {"role": "user", "content": "Need help"} in luna_input["input"]
    if after_tool:
        router.call_tool.assert_awaited_once_with("nodes_list", {}, 42)
        assert "tool result" in luna_input["input"][-1]["content"]
    else:
        router.call_tool.assert_not_awaited()
    faq.build_faq_context.assert_awaited_once()
