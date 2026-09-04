"""Tests for ZaiClient, reasoning matrix, MCP iterations, and error handling."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.llm.base import FaqContext, LlmProcessingException, LlmResponse
from app.llm.fallback import is_fallback_eligible
from app.llm.mcp_client import McpTool
from app.llm.zai import ZaiClient


@pytest.fixture
def make_zai_settings(valid_settings_dict: dict[str, object]):
    def _maker(
        model: str = "glm-5.3-flash",
        effort: str = "none",
        base_url: str = "https://api.z.ai/api/paas/v4",
        timeout: float = 120.0,
    ) -> Settings:
        data = dict(valid_settings_dict)
        data["llm_provider"] = "zai"
        data["zai_api_key"] = "test-zai-secret-key"
        data["zai_model"] = model
        data["zai_base_url"] = base_url
        data["zai_timeout_seconds"] = timeout
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


class TestZaiReasoningMatrix:
    """Validate the exact reasoning matrix for glm-5.3-flash, glm-4.7, glm-5.3, and unknown models."""

    ALL_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

    @pytest.mark.parametrize("model", ["glm-5.3-flash", "glm-4.7"])
    @pytest.mark.parametrize("effort", ALL_EFFORTS)
    @pytest.mark.parametrize("with_tools", [True, False])
    def test_glm_toggle_reasoning(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        model: str,
        effort: str,
        with_tools: bool,
    ) -> None:
        settings = make_zai_settings(model=model, effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = ZaiClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        body = client.build_request_body([{"role": "user", "content": "hello"}])

        if effort == "none":
            assert effective == "none"
            assert body.get("thinking") == {"type": "disabled"}
        else:
            assert effective == "enabled"
            assert body.get("thinking") == {"type": "enabled"}

        # Verify no reasoning_effort, no reasoning object, no clear_thinking
        assert "reasoning_effort" not in body
        assert "reasoning" not in body
        assert "clear_thinking" not in body
        assert "clear_thinking" not in body.get("thinking", {})

        # When tools are present, tool_choice MUST be preserved as auto (do not copy DeepSeek behavior)
        if with_tools:
            assert body.get("tool_choice") == "auto"
            assert "tools" in body
        else:
            assert "tools" not in body
            assert "tool_choice" not in body

    @pytest.mark.parametrize("with_tools", [True, False])
    def test_glm_5_3_none_rejected_before_http(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        with_tools: bool,
    ) -> None:
        settings = make_zai_settings(model="glm-5.3", effort="none")
        router = mock_mcp_router if with_tools else mock_empty_mcp_router

        with pytest.raises((ValueError, LlmProcessingException)) as exc_info:
            ZaiClient(
                settings=settings,
                mcp_router=router,
                chat_history_service=mock_history_service,
                faq_embedding_service=mock_faq_service,
            )

        err_msg = str(exc_info.value)
        assert "Z.AI" in err_msg or "zai" in err_msg.lower()
        assert "glm-5.3" in err_msg
        assert "low" in err_msg.lower()
        # Ensure Settings object was not dumped into the error message
        assert "test-zai-secret-key" not in err_msg
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
        make_zai_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        effort: str,
        expected_effort_param: str,
        expected_effective: str,
        with_tools: bool,
    ) -> None:
        settings = make_zai_settings(model="glm-5.3", effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = ZaiClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        assert effective == expected_effective

        body = client.build_request_body([{"role": "user", "content": "hello"}])
        assert body.get("thinking") == {"type": "enabled"}
        assert body.get("reasoning_effort") == expected_effort_param
        assert "reasoning" not in body
        assert "clear_thinking" not in body

        if with_tools:
            assert body.get("tool_choice") == "auto"
            assert "tools" in body
        else:
            assert "tools" not in body
            assert "tool_choice" not in body

    @pytest.mark.parametrize("effort", ALL_EFFORTS)
    @pytest.mark.parametrize("with_tools", [True, False])
    def test_unknown_model_reasoning_ignored(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_empty_mcp_router,
        mock_history_service,
        mock_faq_service,
        effort: str,
        with_tools: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        settings = make_zai_settings(model="custom-glm-model", effort=effort)
        router = mock_mcp_router if with_tools else mock_empty_mcp_router
        client = ZaiClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
        )

        effective = client.get_effective_reasoning_effort()
        assert effective == "unsupported/ignored"

        with caplog.at_level("INFO"):
            body = client.build_request_body([{"role": "user", "content": "hello"}])

        assert "thinking" not in body
        assert "reasoning_effort" not in body
        assert "reasoning" not in body
        assert any(
            "unsupported/ignored" in record.message or "unsupported" in record.message.lower()
            for record in caplog.records
        )


class TestZaiMcpIterations:
    """Test multi-step MCP loop preserving reasoning_content and linking results."""

    @pytest.mark.asyncio
    async def test_two_consecutive_mcp_iterations_preserve_reasoning_content(
        self,
        make_zai_settings,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        router = MagicMock()
        router.list_tools.return_value = [
            McpTool(name="tool_1", description="Tool 1", input_schema={"type": "object"}),
            McpTool(name="tool_2", description="Tool 2", input_schema={"type": "object"}),
        ]
        router.call_tool = AsyncMock(side_effect=["res_1", "res_2"])

        settings = make_zai_settings(model="glm-4.7", effort="high")

        call_count = 0
        captured_requests: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append(body)

            if call_count == 1:
                # First iteration: tool call with reasoning_content and content=None
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "step 1 thinking process",
                                    "tool_calls": [
                                        {
                                            "id": "tc_alpha_1",
                                            "type": "function",
                                            "function": {
                                                "name": "tool_1",
                                                "arguments": '{"k": 1}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            elif call_count == 2:
                # Second iteration: another tool call with new reasoning_content and multiple tools
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "step 2 deeper thinking",
                                    "tool_calls": [
                                        {
                                            "id": "tc_alpha_2",
                                            "type": "function",
                                            "function": {
                                                "name": "tool_2",
                                                "arguments": '{"k": 2}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            else:
                # Final resolution
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Final user answer from Z.AI",
                                    "reasoning_content": "final internal thought",
                                }
                            }
                        ]
                    },
                )

        client = ZaiClient(
            settings=settings,
            mcp_router=router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        reply = await client.chat("Analyze this for me", telegram_user_id=777)

        assert reply.text == "Final user answer from Z.AI"
        assert call_count == 3
        assert router.call_tool.call_count == 2

        # Check request 2 (contains assistant 1 with preserved reasoning_content and tool result 1)
        req2_messages = captured_requests[1]["messages"]
        asst_msg1 = [m for m in req2_messages if m.get("role") == "assistant"][0]
        assert asst_msg1["reasoning_content"] == "step 1 thinking process"
        assert "reasoning" not in asst_msg1
        assert "reasoning_details" not in asst_msg1
        tool_msg1 = [m for m in req2_messages if m.get("role") == "tool"][0]
        assert tool_msg1["tool_call_id"] == "tc_alpha_1"
        assert tool_msg1["content"] == "res_1"

        # Check request 3 (contains assistant 1 and assistant 2)
        req3_messages = captured_requests[2]["messages"]
        asst_messages = [m for m in req3_messages if m.get("role") == "assistant"]
        assert len(asst_messages) == 2
        assert asst_messages[0]["reasoning_content"] == "step 1 thinking process"
        assert asst_messages[1]["reasoning_content"] == "step 2 deeper thinking"
        assert "reasoning" not in asst_messages[1]
        assert "reasoning_details" not in asst_messages[1]

        # Verify no clear_thinking=false in any request body
        for req in captured_requests:
            assert "clear_thinking" not in req
            if "thinking" in req:
                assert "clear_thinking" not in req["thinking"]

        # Chat history gets only final clean text
        mock_history_service.add_assistant_message.assert_awaited_once_with(
            777, "Final user answer from Z.AI"
        )
        mock_history_service.add_user_message.assert_awaited_once_with(777, "Analyze this for me")


class TestZaiParsingAndValidation:
    """Test response parsing, headers, URL handling, and image support."""

    def test_parse_response_with_reasoning_content(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        client = ZaiClient(
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
                        "content": "Hello user",
                        "reasoning_content": "internal reasoning thoughts",
                    }
                }
            ]
        }
        resp = client.parse_response(payload)
        assert isinstance(resp, LlmResponse)
        assert resp.text == "Hello user"
        assert resp.reasoning_content == "internal reasoning thoughts"

    def test_malformed_last_tool_call_fails_before_executing_first(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        client = ZaiClient(
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
                                "id": "tc_1",
                                "type": "function",
                                "function": {"name": "test_tool", "arguments": '{"arg": "valid"}'},
                            },
                            {
                                "id": "tc_2",
                                "type": "function",
                                "function": {
                                    "name": "test_tool",
                                    "arguments": '{"arg": broken_json',
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

    @pytest.mark.asyncio
    async def test_url_and_auth_headers(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        # Base URL with trailing slash must yield exact endpoint
        settings = make_zai_settings(base_url="https://api.z.ai/api/paas/v4/")

        captured_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_request
            captured_request = request
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "test reply",
                            }
                        }
                    ]
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        assert captured_request is not None
        assert str(captured_request.url) == "https://api.z.ai/api/paas/v4/chat/completions"
        assert captured_request.headers["Authorization"] == "Bearer test-zai-secret-key"
        assert captured_request.headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_image_rejection_before_http(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()

        transport_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal transport_called
            transport_called = True
            return httpx.Response(200, json={})

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert client.supports_images() is False
        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat_with_image("Look at this", 123, "base64data==")

        assert transport_called is False
        assert "не поддерживает обработку изображений" in exc_info.value.user_friendly_message


class TestZaiErrorHandling:
    """Test error handling, business codes (1113, 1210, unknown), and retry behavior."""

    @pytest.mark.asyncio
    async def test_http_429_with_business_code_1113(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        secret_key = "test-zai-secret-key"
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": 1113,
                        "message": f"Balance exhausted sensitive info {secret_key}",
                    }
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        # Retains actual HTTP status
        assert exc.status_code == 429
        # Fallback eligible because 429 is fallback eligible and 1113 is balance exhaustion
        assert is_fallback_eligible(exc) is True
        # HTTP 429 uses the 3 retry attempts of post_with_retry
        assert call_count == 3
        # No leak of raw body or secret keys
        assert secret_key not in str(exc)
        assert secret_key not in exc.user_friendly_message
        assert "Balance exhausted sensitive info" not in str(exc)

    @pytest.mark.asyncio
    async def test_http_400_with_business_code_1210(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 1210,
                        "message": "Invalid parameters passed to model",
                    }
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code == 400
        assert is_fallback_eligible(exc) is False
        assert call_count == 1
        assert "Invalid parameters passed" not in str(exc)

    @pytest.mark.asyncio
    async def test_html_503(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                503,
                text="<html><body>503 Service Temporarily Unavailable</body></html>",
                headers={"Content-Type": "text/html"},
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code == 503
        assert is_fallback_eligible(exc) is True
        assert call_count == 3
        assert "<html>" not in str(exc)

    @pytest.mark.asyncio
    async def test_http_200_with_business_code_1113(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        secret_key = "test-zai-secret-key"
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "error": {
                        "code": 1113,
                        "message": f"Account balance has run out sensitive {secret_key}",
                    }
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        # Critical: on HTTP 200, status_code MUST NOT be set to 1113!
        assert exc.status_code is None
        assert exc.status_code != 1113
        assert is_fallback_eligible(exc) is True
        # No extra hidden retry loop on HTTP 200 body error!
        assert call_count == 1
        assert secret_key not in str(exc)
        assert secret_key not in exc.user_friendly_message

    @pytest.mark.asyncio
    async def test_http_200_with_top_level_code_1113(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "code": 1113,
                    "msg": "Insufficient balance",
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code is None
        assert is_fallback_eligible(exc) is True
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_http_200_with_business_code_1210(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "error": {
                        "code": 1210,
                        "message": "Invalid request parameter model",
                    }
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code is None
        assert is_fallback_eligible(exc) is False
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_http_200_with_unknown_business_code(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "error": {
                        "code": 9999,
                        "message": "Unknown error code",
                    }
                },
            )

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert exc.status_code is None
        assert is_fallback_eligible(exc) is False
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_transport_timeout(
        self,
        make_zai_settings,
        mock_mcp_router,
        mock_history_service,
        mock_faq_service,
    ) -> None:
        settings = make_zai_settings()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("Connection timed out")

        client = ZaiClient(
            settings=settings,
            mcp_router=mock_mcp_router,
            chat_history_service=mock_history_service,
            faq_embedding_service=mock_faq_service,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises((httpx.TimeoutException, LlmProcessingException)) as exc_info:
            await client.call_api([{"role": "user", "content": "hi"}], "faq", 123)

        exc = exc_info.value
        assert is_fallback_eligible(exc) is True
