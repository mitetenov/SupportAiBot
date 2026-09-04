from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.constants import SupportPrompt
from app.llm.base import (
    LlmProcessingException,
    LlmResponse,
    TokenUsage,
    ToolCall,
)
from app.llm.chat_completions import ChatCompletionsClient
from app.llm.fallback import is_fallback_eligible
from app.llm.mcp_router import McpRouter
from app.logging_config import setup_logging
from app.rag.service import FaqContext, FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService


class DummyChatCompletionsClient(ChatCompletionsClient):
    """Minimal concrete implementation of ChatCompletionsClient for testing."""

    def get_provider_name(self) -> str:
        return "DummyProvider"


@pytest.fixture
def mock_mcp_router() -> MagicMock:
    router = MagicMock(spec=McpRouter)
    router.list_tools.return_value = []
    router.call_tool = AsyncMock()
    return router


@pytest.fixture
def mock_chat_history_service() -> MagicMock:
    history_service = MagicMock(spec=ChatHistoryService)
    history_service.get_history = AsyncMock(return_value=[])
    history_service.add_user_message = AsyncMock()
    history_service.add_assistant_message = AsyncMock()
    history_service.add_rejected_faq_questions = MagicMock()
    history_service.clear_rejected_faqs_if_new_topic = MagicMock()
    history_service.get_last_user_message = MagicMock(return_value=None)
    history_service.get_rejected_faq_questions = MagicMock(return_value=[])
    return history_service


@pytest.fixture
def mock_faq_service() -> MagicMock:
    faq_service = MagicMock(spec=FaqEmbeddingService)
    faq_service.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)
    return faq_service


@pytest.fixture
def dummy_client(
    mock_mcp_router: MagicMock,
    mock_chat_history_service: MagicMock,
    mock_faq_service: MagicMock,
) -> DummyChatCompletionsClient:
    return DummyChatCompletionsClient(
        mcp_router=mock_mcp_router,
        chat_history_service=mock_chat_history_service,
        faq_embedding_service=mock_faq_service,
        model="dummy-model",
        base_url="https://api.dummy.com/v1",
        api_key="dummy-secret-key",
        request_timeout_seconds=120.0,
    )


class TestChatCompletionsRequestMessages:
    """Test request body construction, message ordering, and schema preservation."""

    def test_build_initial_conversation_messages_order_and_content(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        user_message = "Привет! Как подключить VPN?"
        user_id = 98765
        faq = "FAQ: Шаги подключения Remnawave"
        history = [
            {"role": "user", "content": "Предыдущий вопрос"},
            {"role": "assistant", "content": "Предыдущий ответ"},
        ]
        history_copy = [dict(msg) for msg in history]

        conv = dummy_client.build_initial_conversation(
            user_message=user_message,
            telegram_user_id=user_id,
            faq_context=faq,
            base64_image=None,
            mime_type=None,
            history=history,
        )

        # 1. First system message is SupportPrompt.SYSTEM
        assert conv[0] == {"role": "system", "content": SupportPrompt.SYSTEM}

        # 2. Second system message is SupportPrompt.dynamic_context containing FAQ and user ID
        assert conv[1]["role"] == "system"
        assert str(user_id) in conv[1]["content"]
        assert faq in conv[1]["content"]

        # 3. History is preserved in order
        assert conv[2] == history[0]
        assert conv[3] == history[1]

        # 4. Input history is not mutated
        assert history == history_copy

        # 5. Last message is user message
        assert conv[4] == {"role": "user", "content": user_message}

    def test_build_initial_conversation_unicode_preservation(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        user_message = "Тест Unicode: 🚀 Привет, мир! 測試"
        conv = dummy_client.build_initial_conversation(
            user_message=user_message,
            telegram_user_id=123,
            faq_context="Вопрос-ответ 💡",
        )
        assert conv[-1]["content"] == user_message
        assert "Вопрос-ответ 💡" in conv[1]["content"]

    def test_build_request_body_empty_tools(self, dummy_client: DummyChatCompletionsClient) -> None:
        messages = [{"role": "user", "content": "hello"}]
        body = dummy_client.build_request_body(messages)

        assert body["model"] == "dummy-model"
        assert body["messages"] == messages
        assert body["stream"] is False
        assert "tools" not in body
        assert "tool_choice" not in body

    def test_build_request_body_with_tools_preserves_schema(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        tool1 = MagicMock()
        tool1.name = "get_server_status"
        tool1.description = "Returns current server status"
        tool1.input_schema = {
            "type": "object",
            "properties": {"server_id": {"type": "string"}},
            "required": ["server_id"],
        }

        tool2 = MagicMock()
        tool2.name = "ping"
        tool2.description = None
        tool2.input_schema = None  # Should get default object schema

        mock_mcp_router.list_tools.return_value = [tool1, tool2]

        client = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            model="dummy-model",
            base_url="https://api.dummy.com/v1",
            api_key="dummy-key",
        )

        messages = [{"role": "user", "content": "check server"}]
        body = client.build_request_body(messages)

        assert body["stream"] is False
        assert body["tool_choice"] == "auto"
        assert len(body["tools"]) == 2

        assert body["tools"][0] == {
            "type": "function",
            "function": {
                "name": "get_server_status",
                "description": "Returns current server status",
                "parameters": tool1.input_schema,
            },
        }
        assert body["tools"][1] == {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        }


class TestChatCompletionsImageSupport:
    """Test that image support is explicitly disabled and rejected before HTTP."""

    def test_supports_images_is_false(self, dummy_client: DummyChatCompletionsClient) -> None:
        assert dummy_client.supports_images() is False

    @pytest.mark.asyncio
    async def test_chat_with_image_raises_before_http(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.chat_with_image(
                user_message="Look at this image",
                telegram_user_id=123,
                base64_image="aW1hZ2VkYXRh",
                mime_type="image/png",
            )

        err = exc_info.value
        assert "Image not supported" in str(err)
        assert "DummyProvider не поддерживает обработку изображений" in err.user_friendly_message


class TestChatCompletionsParsing:
    """Test response parsing: text, tools, think tags, and strict error checking."""

    def test_parse_regular_text(self, dummy_client: DummyChatCompletionsClient) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Обычный текстовый ответ пользователю.",
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert resp.text == "Обычный текстовый ответ пользователю."
        assert resp.tool_calls == []
        assert resp.reasoning_content is None

    def test_parse_strips_think_tags(self, dummy_client: DummyChatCompletionsClient) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>\nThinking step 1\nThinking step 2\n</think>Чистый ответ пользователю.",
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert resp.text == "Чистый ответ пользователю."

    def test_parse_strips_unfinished_think_tag(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>Незавершённая мысль без закрывающего тега",
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert resp.text == ""

    def test_parse_tool_only_with_content_none(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "lookup_user",
                                    "arguments": '{"user_id": 42}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert resp.text == ""
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_123"
        assert resp.tool_calls[0].name == "lookup_user"
        assert resp.tool_calls[0].arguments == {"user_id": 42}

    def test_parse_two_tool_calls_and_nested_arguments(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "tool_one",
                                    "arguments": '{"config": {"nested": [1, 2, "три"]}}',
                                },
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "tool_two",
                                    "arguments": {"already": "dict"},
                                },
                            },
                        ],
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert len(resp.tool_calls) == 2
        assert resp.tool_calls[0].id == "call_1"
        assert resp.tool_calls[0].arguments == {"config": {"nested": [1, 2, "три"]}}
        assert resp.tool_calls[1].id == "call_2"
        assert resp.tool_calls[1].arguments == {"already": "dict"}

    def test_parse_preserves_reasoning_content_if_present(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Answer",
                        "reasoning_content": "Internal thought string",
                    }
                }
            ]
        }
        resp = dummy_client.parse_response(payload)
        assert resp.text == "Answer"
        assert resp.reasoning_content == "Internal thought string"

    @pytest.mark.parametrize(
        ("bad_payload", "expected_err_keyword"),
        [
            ({}, "choices"),
            ({"choices": []}, "choices"),
            ({"choices": [{"message": None}]}, "message"),
            ({"choices": [{}]}, "message"),
            ({"choices": [{"message": {"content": 12345}}]}, "content"),
            ({"choices": [{"message": {"content": ["not", "string"]}}]}, "content"),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": '{"broken: json',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "arguments",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": "[1, 2, 3]",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "object",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": 42,
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "arguments",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "id",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "id",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                "name",
            ),
            (
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_dup",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_1",
                                            "arguments": "{}",
                                        },
                                    },
                                    {
                                        "id": "call_dup",
                                        "type": "function",
                                        "function": {
                                            "name": "tool_2",
                                            "arguments": "{}",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                },
                "Duplicate",
            ),
        ],
    )
    def test_parse_response_strict_validation_errors(
        self,
        dummy_client: DummyChatCompletionsClient,
        bad_payload: dict[str, Any],
        expected_err_keyword: str,
    ) -> None:
        with pytest.raises(LlmProcessingException) as exc_info:
            dummy_client.parse_response(bad_payload)
        assert expected_err_keyword.lower() in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_parse_failure_in_do_chat_prevents_mcp_call(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        dummy_client.call_api = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "dangerous_action",
                                        "arguments": '{"broken json',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

        with pytest.raises(LlmProcessingException):
            await dummy_client.do_chat("Выполни команду", 123)

        assert dummy_client.mcp_router.call_tool.call_count == 0


class TestChatCompletionsConversationHelpers:
    """Test adding assistant tool calls and tool results to conversation history."""

    def test_add_tool_calls_to_conversation(self, dummy_client: DummyChatCompletionsClient) -> None:
        conv: list[dict[str, Any]] = []
        tc = ToolCall(
            name="check_status",
            id="call_status_1",
            arguments={"query": "Тестовый запрос 🌐", "options": [1, 2]},
        )
        resp = LlmResponse(text="", tool_calls=[tc])

        dummy_client.add_tool_calls_to_conversation(conv, resp)

        assert len(conv) == 1
        msg = conv[0]
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert len(msg["tool_calls"]) == 1

        tc_dict = msg["tool_calls"][0]
        assert tc_dict["id"] == "call_status_1"
        assert tc_dict["type"] == "function"
        assert tc_dict["function"]["name"] == "check_status"

        # Check Unicode preservation in serialized arguments
        args_str = tc_dict["function"]["arguments"]
        assert "Тестовый запрос 🌐" in args_str
        assert json.loads(args_str) == {"query": "Тестовый запрос 🌐", "options": [1, 2]}

    def test_add_tool_result_to_conversation(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        conv: list[dict[str, Any]] = []
        tc = ToolCall(name="check_status", id="call_status_1", arguments={})
        result_str = '{"status": "ok", "message": "Сервер работает"}'

        dummy_client.add_tool_result_to_conversation(conv, tc, result_str)

        assert len(conv) == 1
        assert conv[0] == {
            "role": "tool",
            "tool_call_id": "call_status_1",
            "content": result_str,
        }


class TestChatCompletionsHttpTransport:
    """Test HTTP transport, MockTransport, Bearer header, timeouts, and error classification."""

    @pytest.mark.asyncio
    async def test_call_api_success_url_headers_payload(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        captured_request: httpx.Request | None = None

        def handle_request(req: httpx.Request) -> httpx.Response:
            nonlocal captured_request
            captured_request = req
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Success!"}}]},
            )

        transport = httpx.MockTransport(handle_request)
        http_client = httpx.AsyncClient(transport=transport, timeout=30.0)

        client = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=http_client,
            model="dummy-model",
            base_url="https://api.dummy.com/v1/",  # Note trailing slash
            api_key="secret-api-key",
            request_timeout_seconds=90.0,
        )

        conv = [{"role": "user", "content": "hi"}]
        payload = await client.call_api(conv, faq_context="", telegram_user_id=123)

        assert payload["choices"][0]["message"]["content"] == "Success!"
        assert captured_request is not None
        assert captured_request.url == httpx.URL("https://api.dummy.com/v1/chat/completions")
        assert captured_request.headers["Authorization"] == "Bearer secret-api-key"
        assert captured_request.headers["Content-Type"] == "application/json"

        body = json.loads(captured_request.content)
        assert body["model"] == "dummy-model"
        assert body["stream"] is False

    @pytest.mark.asyncio
    async def test_call_api_passes_custom_timeout_override(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded_kwargs: dict[str, Any] = {}

        async def spy_post_with_retry(
            client: httpx.AsyncClient, url: str, **kwargs: Any
        ) -> httpx.Response:
            recorded_kwargs.update(kwargs)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        monkeypatch.setattr("app.llm.chat_completions.post_with_retry", spy_post_with_retry)

        client = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(timeout=15.0),
            model="dummy-model",
            base_url="https://api.dummy.com",
            api_key="key",
            request_timeout_seconds=88.5,
        )

        await client.call_api([{"role": "user", "content": "hi"}], "", 123)
        assert recorded_kwargs.get("timeout") == 88.5
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 429, 500])
    async def test_call_api_http_error_statuses(
        self,
        dummy_client: DummyChatCompletionsClient,
        status_code: int,
    ) -> None:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(status_code, json={"error": {"message": "Fail"}})
        )
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)

        err = exc_info.value
        assert err.status_code == status_code
        assert is_fallback_eligible(err) is True
        # Ensure secret key or raw body is not leaked in exception string
        assert "dummy-secret-key" not in str(err)

    @pytest.mark.asyncio
    async def test_call_api_html_503_preserves_status_code(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        html_body = (
            "<html><head><title>503 Service Unavailable</title></head><body>Error</body></html>"
        )
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(503, text=html_body, headers={"content-type": "text/html"})
        )
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)

        err = exc_info.value
        assert err.status_code == 503
        assert is_fallback_eligible(err) is True
        # Raw HTML should not be in the exception message
        assert "<html>" not in str(err)

    @pytest.mark.asyncio
    async def test_call_api_http_200_with_top_level_error(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"error": {"message": "insufficient balance", "type": "billing_error"}},
            )
        )
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)

        err = exc_info.value
        assert is_fallback_eligible(err) is True
        assert "DummyProvider" in str(err)

    @pytest.mark.asyncio
    async def test_call_api_malformed_json_response(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, text="not a valid json string {")
        )
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)

        assert "malformed json" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_call_api_json_array_instead_of_object(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, text='[{"not": "object"}]')
        )
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        with pytest.raises(LlmProcessingException) as exc_info:
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)

        assert "expected an object" in str(exc_info.value).lower()


class TestChatCompletionsOwnershipAndLifecycle:
    """Test lazy client creation, injected client ownership, and cancellation handling."""

    @pytest.mark.asyncio
    async def test_lazy_http_client_creation_and_close(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        client = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            model="dummy-model",
            base_url="https://api.dummy.com",
            api_key="key",
        )

        assert client._http_client is None
        http_c = client.http_client
        assert isinstance(http_c, httpx.AsyncClient)
        assert client._own_client is True
        assert not http_c.is_closed

        await client.close()
        assert http_c.is_closed

    def test_lazy_http_client_has_logging_hooks(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        client = dummy_client.http_client
        assert "response" in client.event_hooks
        assert len(client.event_hooks["response"]) > 0

    @pytest.mark.asyncio
    async def test_injected_http_client_remains_open_on_close(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        injected = httpx.AsyncClient()
        client = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=injected,
            model="dummy-model",
            base_url="https://api.dummy.com",
            api_key="key",
        )

        assert client.http_client is injected
        assert client._own_client is False

        await client.close()
        assert not injected.is_closed
        await injected.aclose()

    @pytest.mark.asyncio
    async def test_two_clients_sharing_http_client_do_not_mutate_it(
        self,
        mock_mcp_router: MagicMock,
        mock_chat_history_service: MagicMock,
        mock_faq_service: MagicMock,
    ) -> None:
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )
        )
        shared_http = httpx.AsyncClient(
            transport=transport,
            timeout=45.0,
            headers={"X-Shared": "constant"},
        )

        client1 = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=shared_http,
            model="model-1",
            base_url="https://api1.dummy.com",
            api_key="key-1",
            request_timeout_seconds=60.0,
        )

        client2 = DummyChatCompletionsClient(
            mcp_router=mock_mcp_router,
            chat_history_service=mock_chat_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=shared_http,
            model="model-2",
            base_url="https://api2.dummy.com",
            api_key="key-2",
            request_timeout_seconds=90.0,
        )

        initial_headers = dict(shared_http.headers)
        initial_timeout = shared_http.timeout

        await client1.call_api([{"role": "user", "content": "1"}], "", 123)
        await client2.call_api([{"role": "user", "content": "2"}], "", 123)

        assert dict(shared_http.headers) == initial_headers
        assert shared_http.timeout == initial_timeout

        await client1.close()
        await client2.close()
        assert not shared_http.is_closed
        await shared_http.aclose()

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_without_processing_exception(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        def raise_cancelled(_req: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError("Turn was cancelled")

        dummy_client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(raise_cancelled)
        )
        dummy_client._own_client = True

        with pytest.raises(asyncio.CancelledError):
            await dummy_client.call_api([{"role": "user", "content": "hi"}], "", 123)


class TestChatCompletionsTokenUsage:
    """Test token usage extraction: full, absent, partial, reasoning counters, and resilience."""

    def test_extract_usage_full(self, dummy_client: DummyChatCompletionsClient) -> None:
        payload = {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "total_tokens": 165,
            }
        }
        usage = dummy_client.extract_usage(payload)
        assert usage == TokenUsage(prompt_tokens=120, completion_tokens=45, total_tokens=165)

    def test_extract_usage_absent(self, dummy_client: DummyChatCompletionsClient) -> None:
        assert dummy_client.extract_usage({}) is None
        assert dummy_client.extract_usage({"usage": None}) is None
        assert dummy_client.extract_usage({"usage": "not a dict"}) is None

    def test_extract_usage_partial_calculates_total(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
            }
        }
        usage = dummy_client.extract_usage(payload)
        assert usage == TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)

    def test_extract_usage_does_not_double_count_reasoning_or_cached_tokens(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 20},
                "prompt_tokens_details": {"cached_tokens": 30},
            }
        }
        usage = dummy_client.extract_usage(payload)
        assert usage == TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)

    def test_extract_usage_resilient_to_negative_or_non_numeric(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "usage": {
                "prompt_tokens": -10,
                "completion_tokens": "invalid",
                "total_tokens": 100,
            }
        }
        usage = dummy_client.extract_usage(payload)
        assert usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=100)

    def test_extract_usage_all_invalid_returns_none(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        payload = {
            "usage": {
                "prompt_tokens": -10,
                "completion_tokens": "abc",
                "total_tokens": -5,
            }
        }
        assert dummy_client.extract_usage(payload) is None

    @pytest.mark.asyncio
    async def test_invalid_usage_in_do_chat_does_not_break_reply(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        # Mock call_api returning normal choice with invalid usage
        dummy_client.call_api = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Всё работает отлично!",
                        }
                    }
                ],
                "usage": {"prompt_tokens": "invalid", "total_tokens": -99},
            }
        )
        reply = await dummy_client.do_chat("Привет", 123)
        assert reply.text == "Всё работает отлично!"


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "error", "unknown"])
def test_explicit_non_success_finish_reason_is_rejected(
    dummy_client: DummyChatCompletionsClient, finish_reason: str | None
) -> None:
    payload = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": "partial", "tool_calls": []},
            }
        ]
    }
    with pytest.raises(LlmProcessingException):
        dummy_client.parse_response(payload)


class TestChatCompletionsLoggingSafety:
    """Test that INFO and ERROR logging do not leak request bodies, API keys, or raw payloads."""

    @pytest.mark.asyncio
    async def test_info_level_does_not_log_request_payload_or_key(
        self, dummy_client: DummyChatCompletionsClient
    ) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        secret_prompt = "TOP_SECRET_USER_PROMPT_999"
        secret_reply = "TOP_SECRET_MODEL_REPLY_888"

        def handle_request(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": secret_reply}}]},
            )

        transport = httpx.MockTransport(handle_request)
        dummy_client._http_client = httpx.AsyncClient(transport=transport)
        dummy_client._own_client = True

        conv = dummy_client.build_initial_conversation(secret_prompt, 123)
        payload = await dummy_client.call_api(conv, "", 123)
        resp = dummy_client.parse_response(payload)

        assert resp.text == secret_reply
        logs = stream.getvalue()
        assert secret_prompt not in logs
        assert secret_reply not in logs
        assert "dummy-secret-key" not in logs
