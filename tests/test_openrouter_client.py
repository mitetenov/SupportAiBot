"""Tests for OpenRouterClient, OpenRouterResponse, reasoning matrix, MCP iterations, and error handling."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.llm.base import FaqContext, LlmProcessingException
from app.llm.fallback import is_fallback_eligible
from app.llm.mcp_client import McpTool
from app.llm.openrouter import OpenRouterClient, OpenRouterResponse


@pytest.fixture
def make_openrouter_settings(valid_settings_dict: dict[str, object]):
    def _maker(
        model: str = "z-ai/glm-4.7",
        effort: str = "none",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 120.0,
    ) -> Settings:
        data = dict(valid_settings_dict)
        data["llm_provider"] = "openrouter"
        data["openrouter_api_key"] = "test-openrouter-secret-key"
        data["openrouter_model"] = model
        data["openrouter_base_url"] = base_url
        data["openrouter_timeout_seconds"] = timeout
        data["reasoning_effort"] = effort
        return Settings(**data)

    return _maker


@pytest.fixture
def mock_mcp_router() -> MagicMock:
    router = MagicMock()
    router.list_tools.return_value = [
        McpTool(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"arg": {"type": "string"}},
                "required": ["arg"],
            },
        )
    ]
    router.call_tool = AsyncMock(return_value="tool_result_ok")
    return router


@pytest.fixture
def mock_empty_mcp_router() -> MagicMock:
    router = MagicMock()
    router.list_tools.return_value = []
    router.call_tool = AsyncMock()
    return router


@pytest.fixture
def mock_history_service() -> MagicMock:
    svc = MagicMock()
    svc.get_history = AsyncMock(return_value=[])
    svc.get_last_user_message.return_value = None
    svc.get_rejected_faq_questions.return_value = set()
    svc.add_user_message = AsyncMock()
    svc.add_assistant_message = AsyncMock()
    svc.add_rejected_faq_questions = MagicMock()
    svc.clear_rejected_faqs_if_new_topic = MagicMock()
    return svc


@pytest.fixture
def mock_faq_service() -> MagicMock:
    svc = MagicMock()
    svc.find_relevant_faqs = AsyncMock(return_value=None)
    svc.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)
    return svc


class TestOpenRouterReasoningMatrix:
    """Validate the exact reasoning matrix for glm-4.7, glm-5.3, and unknown models."""

    ALL_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

    @pytest.mark.parametrize("effort", ALL_EFFORTS)
    @pytest.mark.parametrize("with_tools", [True, False])
    def test_glm_4_7_reasoning(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        effort: str,
        with_tools: bool,
    ) -> None:
        settings = make_openrouter_settings(model="z-ai/glm-4.7", effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = OpenRouterClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        body = client.build_request_body([{"role": "user", "content": "hello"}])

        if effort == "none":
            assert effective == "none"
            assert body.get("reasoning") == {"enabled": False}
        else:
            assert effective == "enabled"
            assert body.get("reasoning") == {"enabled": True}

        # Verify no forbidden parameters
        assert "exclude" not in body.get("reasoning", {})
        assert "thinking" not in body
        assert "input" not in body
        assert "instructions" not in body

    @pytest.mark.parametrize("with_tools", [True, False])
    def test_glm_5_3_none_rejected_before_http(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        with_tools: bool,
    ) -> None:
        settings = make_openrouter_settings(model="z-ai/glm-5.3", effort="none")
        router = mock_mcp_router if with_tools else mock_empty_mcp_router

        with pytest.raises((ValueError, LlmProcessingException)) as exc_info:
            OpenRouterClient(
                settings=settings,
                mcp_router=router,
                chat_history_service=mock_history_service,
                faq_embedding_service=mock_faq_service,
            )

        err_msg = str(exc_info.value)
        assert "OpenRouter" in err_msg or "openrouter" in err_msg.lower()
        assert "z-ai/glm-5.3" in err_msg
        assert "low" in err_msg.lower()
        # Ensure Settings object was not dumped into the error message
        assert "test-openrouter-secret-key" not in err_msg
        assert "telegram_bot_token" not in err_msg

    @pytest.mark.parametrize(
        ("effort", "expected_effort_param", "expected_effective"),
        [
            ("minimal", "low", "low"),
            ("low", "low", "low"),
            ("medium", "high", "high"),
            ("high", "high", "high"),
            ("xhigh", "max", "max"),
            ("max", "max", "max"),
        ],
    )
    @pytest.mark.parametrize("with_tools", [True, False])
    def test_glm_5_3_efforts(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        effort: str,
        expected_effort_param: str,
        expected_effective: str,
        with_tools: bool,
    ) -> None:
        settings = make_openrouter_settings(model="z-ai/glm-5.3", effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = OpenRouterClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        assert effective == expected_effective

        body = client.build_request_body([{"role": "user", "content": "hello"}])
        assert body.get("reasoning") == {"effort": expected_effort_param}
        assert "exclude" not in body["reasoning"]
        assert "thinking" not in body

    @pytest.mark.parametrize("effort", ALL_EFFORTS)
    @pytest.mark.parametrize("with_tools", [True, False])
    def test_unknown_model_reasoning_ignored(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        effort: str,
        with_tools: bool,
    ) -> None:
        settings = make_openrouter_settings(model="openai/gpt-4o", effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = OpenRouterClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        assert effective == "unsupported/ignored"

        body = client.build_request_body([{"role": "user", "content": "hello"}])
        assert "reasoning" not in body


class TestOpenRouterMcpIterations:
    """Test multi-step MCP loop preserving reasoning and reasoning_details."""

    @pytest.mark.asyncio
    async def test_two_consecutive_mcp_iterations_preserve_reasoning_details(
        self,
        make_openrouter_settings,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        router = MagicMock()
        router.list_tools.return_value = [
            McpTool(name="tool_1", description="Tool 1", input_schema={"type": "object"}),
            McpTool(name="tool_2", description="Tool 2", input_schema={"type": "object"}),
        ]
        router.call_tool = AsyncMock(side_effect=["res_1", "res_2"])

        settings = make_openrouter_settings(model="z-ai/glm-4.7", effort="high")

        call_count = 0
        captured_requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append(body)

            # Assert headers: only Authorization Bearer and Content-Type, no attribution
            assert request.headers["Authorization"] == "Bearer test-openrouter-secret-key"
            assert "HTTP-Referer" not in request.headers
            assert "X-Title" not in request.headers

            if call_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "tc_alpha",
                                            "type": "function",
                                            "function": {
                                                "name": "tool_1",
                                                "arguments": '{"query": "first"}',
                                            },
                                        }
                                    ],
                                    "reasoning": "step 1 thinking",
                                    "reasoning_details": [
                                        {
                                            "type": "reasoning.encrypted",
                                            "data": "cipher_blob",
                                            "signature": "sig_001",
                                            "extra_unknown_field": 42,
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            elif call_count == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "tc_beta",
                                            "type": "function",
                                            "function": {
                                                "name": "tool_2",
                                                "arguments": '{"query": "second"}',
                                            },
                                        }
                                    ],
                                    "reasoning": "step 2 thinking",
                                    "reasoning_details": [
                                        {
                                            "type": "reasoning.text",
                                            "text": "plain thinking detail",
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            else:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Final resolution for user",
                                }
                            }
                        ]
                    },
                )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        reply = await client.chat("Help me", telegram_user_id=999)

        assert reply.text == "Final resolution for user"
        assert call_count == 3
        assert router.call_tool.call_count == 2

        # Check second HTTP request (contains assistant message from iter 1 + tool result 1)
        req2_messages = captured_requests[1]["messages"]
        asst_msg1 = [m for m in req2_messages if m.get("role") == "assistant"][0]
        assert asst_msg1["reasoning"] == "step 1 thinking"
        assert asst_msg1["reasoning_details"] == [
            {
                "type": "reasoning.encrypted",
                "data": "cipher_blob",
                "signature": "sig_001",
                "extra_unknown_field": 42,
            }
        ]
        assert "reasoning_content" not in asst_msg1
        tool_msg1 = [m for m in req2_messages if m.get("role") == "tool"][0]
        assert tool_msg1["tool_call_id"] == "tc_alpha"
        assert tool_msg1["content"] == "res_1"

        # Check third HTTP request (contains assistant messages 1 and 2)
        req3_messages = captured_requests[2]["messages"]
        asst_messages = [m for m in req3_messages if m.get("role") == "assistant"]
        assert len(asst_messages) == 2
        asst_msg2 = asst_messages[1]
        assert asst_msg2["reasoning"] == "step 2 thinking"
        assert asst_msg2["reasoning_details"] == [
            {
                "type": "reasoning.text",
                "text": "plain thinking detail",
            }
        ]
        assert "reasoning_content" not in asst_msg2

        # Verify chat history was saved with clean final text
        mock_history_service.add_assistant_message.assert_awaited_once_with(
            999, "Final resolution for user"
        )
        mock_history_service.add_user_message.assert_awaited_once_with(999, "Help me")


class TestOpenRouterParsingAndValidation:
    """Test strict parsing of OpenRouterResponse and tool calls."""

    def test_parse_openrouter_response_with_details(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_openrouter_settings()
        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Here is the response",
                        "reasoning": "thought process",
                        "reasoning_details": [
                            {"type": "signature", "data": "abc123xyz"},
                            {"type": "text", "text": "detail"},
                        ],
                    }
                }
            ]
        }
        resp = client.parse_response(payload)
        assert isinstance(resp, OpenRouterResponse)
        assert resp.text == "Here is the response"
        assert resp.reasoning_content == "thought process"
        assert resp.reasoning_details == [
            {"type": "signature", "data": "abc123xyz"},
            {"type": "text", "text": "detail"},
        ]

    def test_malformed_last_tool_call_fails_before_executing_first(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_openrouter_settings()
        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

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
                                "function": {"name": "test_tool", "arguments": '{"arg": "valid"}'},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "test_tool",
                                    "arguments": '{"arg": invalid_json',
                                },
                            },
                        ],
                    }
                }
            ]
        }
        with pytest.raises(LlmProcessingException) as exc_info:
            client.parse_response(payload)

        assert "malformed" in str(exc_info.value).lower()
        mock_mcp_router.call_tool.assert_not_called()


class TestOpenRouterErrorHandling:
    """Test HTTP 200 body-errors, status normalization, and fallback eligibility."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code", "expected_status", "expected_fallback"),
        [(404, 404, False), ("422", 422, False), (502, 502, True)],
    )
    async def test_choice_error_envelope_cancels_partial_response(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
        code: int | str,
        expected_status: int,
        expected_fallback: bool,
    ) -> None:
        settings = make_openrouter_settings()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "error",
                            "error": {"code": code, "message": "private provider detail"},
                            "message": {
                                "content": "partial answer",
                                "tool_calls": [],
                            },
                        }
                    ]
                },
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)
        exc = exc_info.value
        assert exc.status_code == expected_status
        assert is_fallback_eligible(exc) is expected_fallback
        assert "private provider detail" not in str(exc)

    @pytest.mark.parametrize(
        ("body_code", "expected_status", "expected_fallback"),
        [
            (402, 402, True),
            ("429", 429, True),
            (400, 400, False),
            ("unknown_code", None, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_body_error_at_200(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
        body_code: Any,
        expected_status: int | None,
        expected_fallback: bool,
    ) -> None:
        secret_key = "test-openrouter-secret-key"
        settings = make_openrouter_settings()

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "error": {
                        "code": body_code,
                        "message": f"Provider error with sensitive payload {secret_key}",
                    }
                },
            )

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code == expected_status
        assert is_fallback_eligible(exc) is expected_fallback

        # No double retry on HTTP 200 with body error
        assert call_count == 1

        # No credential or raw message leak
        assert secret_key not in str(exc)
        assert secret_key not in exc.user_friendly_message
        assert "Provider error" not in str(exc)

    @pytest.mark.asyncio
    async def test_image_rejection_before_http(
        self,
        make_openrouter_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_openrouter_settings()

        transport_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal transport_called
            transport_called = True
            return httpx.Response(200, json={})

        client = OpenRouterClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert client.supports_images() is False
        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat_with_image("Describe this", 123, "base64data==")

        assert transport_called is False
        assert "не поддерживает обработку изображений" in exc_info.value.user_friendly_message
