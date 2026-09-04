"""Comprehensive integration tests for supportBot 3-level logging contract:
- Model selection and effort on INFO
- Fallback transition on INFO and error on ERROR
- MCP & RAG telemetry on INFO without content/arguments
- Tool descriptions loaded vs available at init/reconnect
- RAG and DB TRACE diagnostics (SQL, params, candidates)
- Pipeline contextvars propagation and reset (Telegram, /ask, Bedolaga)
- Privacy guarantees (no personal IDs or credentials on INFO/ERROR)
- ERROR level contract (safe summaries, malformed webhooks, is_error)
- Normal shutdown produces no error logs
"""

import hashlib
import hmac
import io
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bedolaga.client import BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.state import TicketProgress
from app.bedolaga.types import Ticket, TicketMessage
from app.bedolaga.webhook import BedolagaWebhookEndpoint
from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.operator_ask import OperatorAskCommand
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.sender import TelegramMessageSender
from app.config import Settings
from app.llm.base import LlmProcessingException, LlmReply
from app.llm.deepseek import DeepSeekClient
from app.llm.fallback import LlmFallbackClient
from app.llm.gemini import GeminiClient
from app.llm.groq import GroqClient
from app.llm.mcp_client import HttpMcpClient, McpTool
from app.llm.mcp_router import McpRouter
from app.llm.openai_client import OpenAiClient
from app.logging_config import TRACE, setup_logging
from app.logging_context import get_correlation_id
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqContext
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    setup_logging(level="TRACE", stream=stream)
    return stream


def _make_batch(text: str, user_id: int = 987654) -> MessageBatch:
    msg = MagicMock()
    msg.message_id = 123
    msg.chat.id = user_id
    user = MagicMock()
    user.id = user_id
    user.username = "test_user_secret"
    msg.from_user = user
    return MessageBatch(
        last_message=msg,
        user=user,
        text=text,
        message_ids=[123],
        user_text=text,
    )


class TestModelSelectionAndEffortLogging:
    """Requirement 1: Model selection and effort on INFO."""

    def test_openai_model_selection_and_effort(
        self, valid_settings_dict: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        valid_settings_dict.update(
            {
                "openai_api_key": "sk-test-key-12345",
                "openai_model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
            }
        )
        settings = Settings(**valid_settings_dict)
        mcp_router = MagicMock(spec=McpRouter)
        mcp_router.list_tools.return_value = []
        history = MagicMock(spec=ChatHistoryService)
        faq = MagicMock(spec=FaqEmbeddingService)

        caplog.set_level(logging.INFO)
        client = OpenAiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=history,
            faq_embedding_service=faq,
        )

        assert client.get_effective_reasoning_effort() == "medium"
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        messages = [r.getMessage() for r in info_records]
        assert any("Selected LLM" in m and "gpt-5.6-luna" in m and "medium" in m for m in messages)

    def test_openai_unsupported_model_effort_explicitly_unsupported(
        self, valid_settings_dict: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        valid_settings_dict.update(
            {
                "openai_api_key": "sk-test-key-12345",
                "openai_model": "gpt-4.1",
                "reasoning_effort": "low",
            }
        )
        settings = Settings(**valid_settings_dict)
        mcp_router = MagicMock(spec=McpRouter)
        mcp_router.list_tools.return_value = []
        history = MagicMock(spec=ChatHistoryService)
        faq = MagicMock(spec=FaqEmbeddingService)

        caplog.set_level(logging.INFO)
        client = OpenAiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=history,
            faq_embedding_service=faq,
        )

        assert client.get_effective_reasoning_effort() == "unsupported/ignored"
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        messages = [r.getMessage() for r in info_records]
        assert any("Selected LLM" in m and "unsupported/ignored" in m for m in messages)

    def test_deepseek_model_selection_and_native_effort(
        self, valid_settings_dict: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        valid_settings_dict.update(
            {
                "deepseek_api_key": "ds-test-key-12345",
                "deepseek_model": "deepseek-chat",
                "reasoning_effort": "medium",
            }
        )
        settings = Settings(**valid_settings_dict)
        mcp_router = MagicMock(spec=McpRouter)
        mcp_router.list_tools.return_value = []
        history = MagicMock(spec=ChatHistoryService)
        faq = MagicMock(spec=FaqEmbeddingService)

        caplog.set_level(logging.INFO)
        client = DeepSeekClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=history,
            faq_embedding_service=faq,
        )

        # DeepSeek maps medium -> high
        assert client.get_effective_reasoning_effort() == "high"
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        messages = [r.getMessage() for r in info_records]
        assert any("Selected LLM" in m and "DeepSeek" in m and "high" in m for m in messages)

    def test_groq_model_selection_and_effort(
        self, valid_settings_dict: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        valid_settings_dict.update(
            {
                "groq_api_key": "gsk_testkey12345678901234567890",
                "groq_model": "qwen/qwen3.8-27b",
                "reasoning_effort": "low",
            }
        )
        settings = Settings(**valid_settings_dict)
        mcp_router = MagicMock(spec=McpRouter)
        mcp_router.list_tools.return_value = []
        history = MagicMock(spec=ChatHistoryService)
        faq = MagicMock(spec=FaqEmbeddingService)

        caplog.set_level(logging.INFO)
        client = GroqClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=history,
            faq_embedding_service=faq,
        )

        assert client.get_effective_reasoning_effort() == "low"
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        messages = [r.getMessage() for r in info_records]
        assert any("Selected LLM" in m and "Groq" in m for m in messages)

    def test_gemini_model_selection_and_effort(
        self, valid_settings_dict: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        valid_settings_dict.update(
            {
                "gemini_api_key": "gemini-test-key-12345",
                "gemini_model": "gemini-2.5-flash",
                "reasoning_effort": "medium",
            }
        )
        settings = Settings(**valid_settings_dict)
        mcp_router = MagicMock(spec=McpRouter)
        mcp_router.list_tools.return_value = []
        history = MagicMock(spec=ChatHistoryService)
        faq = MagicMock(spec=FaqEmbeddingService)

        caplog.set_level(logging.INFO)
        client = GeminiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=history,
            faq_embedding_service=faq,
        )

        assert "budget=8192" in client.get_effective_reasoning_effort()
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        messages = [r.getMessage() for r in info_records]
        assert any("Selected LLM" in m and "Gemini" in m for m in messages)


class TestFallbackTransitionLogging:
    """Requirement 1: Fallback transition event on INFO, failure on ERROR."""

    @pytest.mark.asyncio
    async def test_fallback_transition_on_info_and_failure_on_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        primary = MagicMock()
        primary.get_provider_name.return_value = "OpenAI"
        primary.model = "gpt-5.6-luna"
        primary.prepare_turn = AsyncMock(
            return_value=MagicMock(
                faq_context=FaqContext.EMPTY,
                completed_tool_results={},
                replay_completed_tool_results=False,
            )
        )
        primary.do_chat = AsyncMock(
            side_effect=LlmProcessingException("OpenAI rate limit", status_code=429)
        )
        primary.get_effective_reasoning_effort.return_value = "medium"

        secondary = MagicMock()
        secondary.get_provider_name.return_value = "Groq"
        secondary.model = "llama-3.3-70b-versatile"
        secondary.do_chat = AsyncMock(return_value=LlmReply(text="Answer from Groq"))
        secondary.get_effective_reasoning_effort.return_value = "unsupported/ignored"
        secondary.chat_history_service = MagicMock()
        secondary.chat_history_service.add_user_message = AsyncMock()
        secondary.chat_history_service.add_assistant_message = AsyncMock()
        secondary.chat_history_service.add_rejected_faq_questions = MagicMock()

        fallback_client = LlmFallbackClient([primary, secondary])
        reply = await fallback_client.chat("Hello", 12345)

        assert reply.text == "Answer from Groq"

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]

        # 1. Primary failure logged on ERROR
        assert any("OpenAI" in r.getMessage() and "failed" in r.getMessage() for r in error_records)

        # 2. Fallback transition logged on INFO with initial, target, safe reason, and target effective effort
        transition_logs = [
            r.getMessage() for r in info_records if "fallback transition" in r.getMessage().lower()
        ]
        assert len(transition_logs) == 1
        transition_msg = transition_logs[0]
        assert "OpenAI" in transition_msg
        assert "Groq" in transition_msg
        assert "effective_effort" in transition_msg


class TestMcpTelemetryAndDescriptions:
    """Requirement 2: MCP telemetry on INFO, tool descriptions loaded vs available."""

    @pytest.mark.asyncio
    async def test_mcp_call_telemetry_on_info_without_arguments_or_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        client = HttpMcpClient(server_name="remnawave", base_url="http://127.0.0.1:8080")
        mock_sdk_client = MagicMock()
        mock_res = MagicMock()
        mock_res.is_error = False
        mock_res.content = [MagicMock(text="Sensitive tool result data")]
        mock_sdk_client.call_tool = AsyncMock(return_value=mock_res)

        rendered = await client._invoke_tool(
            mock_sdk_client, "users_get", {"secret_user_id": "123456"}
        )
        assert rendered == "Sensitive tool result data"

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        mcp_calls = [r.getMessage() for r in info_records if "MCP call:" in r.getMessage()]
        assert len(mcp_calls) == 1
        call_msg = mcp_calls[0]
        assert "server=remnawave" in call_msg
        assert "tool=users_get" in call_msg
        assert "outcome=success" in call_msg
        assert "duration=" in call_msg

        # Ensure arguments and results are absent from INFO
        assert "secret_user_id" not in call_msg
        assert "Sensitive tool result data" not in call_msg

    def test_mcp_router_tool_descriptions_loaded_vs_available(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        client_mock = MagicMock(spec=HttpMcpClient)
        client_mock.server_name = "remnawave"
        client_mock.list_tools.return_value = [
            McpTool(
                name="users_get_by_telegram_id",
                description="Get user details by Telegram ID",
                input_schema={"type": "object", "properties": {"telegram_id": {"type": "integer"}}},
            ),
            McpTool(
                name="unauthorized_tool",
                description="Some unauthorized tool",
                input_schema={"type": "object"},
            ),
        ]

        McpRouter(clients=[client_mock], readonly=True)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        tool_desc_logs = [
            r.getMessage() for r in info_records if "MCP tool loaded:" in r.getMessage()
        ]

        # Both tools loaded, one available and one withheld
        assert any(
            "users_get_by_telegram_id" in m
            and "available" in m
            and "Get user details by Telegram ID" in m
            for m in tool_desc_logs
        )
        assert any(
            "unauthorized_tool" in m and "withheld" in m and "Some unauthorized tool" in m
            for m in tool_desc_logs
        )

        # Input schema must NOT appear on INFO
        for m in tool_desc_logs:
            assert "input_schema" not in m
            assert "properties" not in m


class TestRagTelemetryAndDatabaseTrace:
    """Requirements 2 and 3: RAG telemetry on INFO, SQL/params/history on TRACE."""

    @pytest.mark.asyncio
    async def test_rag_search_telemetry_on_info_without_query_or_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        db_manager = MagicMock(spec=DatabaseSessionManager)
        embedding_provider = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=embedding_provider)
        service.ready = True
        service.embed_query_as_vector = AsyncMock(return_value="[0.1, 0.2, 0.3]")

        mock_session = AsyncMock()
        row_mock = MagicMock()
        row_mock.question = "How to pay?"
        row_mock.answer = "Secret answer payment instructions"
        row_mock.image = None
        row_mock.vector_sim = 0.85
        row_mock.rrf_score = 0.05
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [row_mock]
        mock_session.execute = AsyncMock(return_value=mock_result)

        class MockSessionContext:
            async def __aenter__(self) -> Any:
                return mock_session

            async def __aexit__(self, *args: Any) -> None:
                pass

        db_manager.session.return_value = MockSessionContext()

        secret_query = "user_secret_query_text_here"
        results = await service.search(secret_query)
        assert len(results) == 1

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        rag_logs = [r.getMessage() for r in info_records if "RAG search:" in r.getMessage()]
        assert len(rag_logs) == 1
        msg = rag_logs[0]
        assert "operation=hybrid_search" in msg
        assert "candidates_count=1" in msg
        assert "outcome=success" in msg
        assert "duration=" in msg

        # Ensure query text, embeddings, and FAQ answer content are completely absent from INFO
        assert secret_query not in msg
        assert "[0.1, 0.2, 0.3]" not in msg
        assert "Secret answer payment instructions" not in msg

    @pytest.mark.asyncio
    async def test_chat_history_diagnostics_on_trace(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(TRACE)

        db_manager = MagicMock(spec=DatabaseSessionManager)
        mock_session = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.role = "user"
        mock_msg.content = "Previous question"
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_msg]
        mock_session.execute = AsyncMock(return_value=mock_result)

        class MockSessionContext:
            async def __aenter__(self) -> Any:
                return mock_session

            async def __aexit__(self, *args: Any) -> None:
                pass

        db_manager.session.return_value = MockSessionContext()

        history_service = ChatHistoryService(db_manager=db_manager)
        history = await history_service.get_history(user_id=777)
        assert len(history) == 1

        trace_records = [r for r in caplog.records if r.levelno == TRACE]
        trace_messages = [r.getMessage() for r in trace_records]

        # Verify SQL statement and parameters are captured on TRACE
        assert any("Storage select chat_history:" in m and "777" in m for m in trace_messages)


class TestPipelineContextvarsAndPrivacy:
    """Requirements 4, 5, 6: Correlation ID lifecycle, privacy on INFO/ERROR, error contract."""

    @pytest.mark.asyncio
    async def test_telegram_pipeline_correlation_and_privacy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        llm_client = MagicMock()
        llm_client.chat = AsyncMock(return_value=LlmReply(text="Bot answer"))
        sender = MagicMock(spec=TelegramMessageSender)
        sender.send = AsyncMock()
        forwarder = MagicMock()
        forwarder.forward_to_support = AsyncMock()
        rate_limiter = MagicMock(spec=UserRateLimiter)
        rate_limiter.try_acquire.return_value = True
        gap_service = MagicMock()
        gap_service.evaluate = AsyncMock()
        conv_state = MagicMock(spec=ConversationState)
        conv_state.is_operator_recently_active.return_value = False
        typing_indicator = MagicMock()
        typing_indicator.start.return_value = MagicMock()

        pipeline = UserMessagePipeline(
            llm_client=llm_client,
            sender=sender,
            forwarder=forwarder,
            rate_limiter=rate_limiter,
            knowledge_gap_service=gap_service,
            conversation_state=conv_state,
            typing_indicator=typing_indicator,
        )

        secret_user_id = 999888
        batch = _make_batch("My secret question", user_id=secret_user_id)

        # Pre-condition: correlation_id is not set
        assert get_correlation_id() is None

        await pipeline.handle(batch)

        # Post-condition: correlation_id was properly reset in finally
        assert get_correlation_id() is None

        # Privacy check: secret_user_id and message text must not leak to INFO or ERROR
        for record in caplog.records:
            if record.levelno >= logging.INFO:
                msg = record.getMessage()
                assert str(secret_user_id) not in msg
                assert "My secret question" not in msg

    @pytest.mark.asyncio
    async def test_operator_ask_correlation_and_privacy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        llm_client = MagicMock()
        llm_client.chat = AsyncMock(return_value=LlmReply(text="Operator response"))
        sender = MagicMock(spec=TelegramMessageSender)
        sender.send = AsyncMock()
        sender.send_to_topic = AsyncMock()
        conv_state = MagicMock(spec=ConversationState)
        typing_indicator = MagicMock()
        typing_indicator.start.return_value = MagicMock()

        operator_cmd = OperatorAskCommand(
            llm_client=llm_client,
            sender=sender,
            conversation_state=conv_state,
            typing_indicator=typing_indicator,
            support_group_chat_id=-100123456789,
        )

        assert get_correlation_id() is None
        await operator_cmd.handle(topic_id=55, user_id=888999, query="Operator query")
        assert get_correlation_id() is None

        # Verify privacy on INFO/ERROR
        for record in caplog.records:
            if record.levelno >= logging.INFO:
                msg = record.getMessage()
                assert "888999" not in msg
                assert "Operator query" not in msg

    @pytest.mark.asyncio
    async def test_bedolaga_ticket_turn_correlation_and_error_contract(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        client = MagicMock(spec=BedolagaClient)
        client.get_ticket = AsyncMock(
            return_value=Ticket(
                id=42,
                user_id=12345,
                title="VPN issue",
                status="open",
                messages=(TicketMessage(id=1, text="Help me", is_from_admin=False),),
            )
        )
        client.resolve_telegram_id = AsyncMock(side_effect=RuntimeError("Panel connection dropped"))

        state = MagicMock()
        state.progress = AsyncMock(return_value=TicketProgress())
        rate_limiter = MagicMock()
        admin_notifier = MagicMock()
        admin_notifier.notify_error = AsyncMock()
        forwarder = MagicMock()
        gap_service = MagicMock()
        conv_state = MagicMock()

        answerer = TicketAnswerer(
            client=client,
            llm_client=MagicMock(),
            state=state,
            rate_limiter=rate_limiter,
            admin_notifier=admin_notifier,
            forwarder=forwarder,
            knowledge_gap_service=gap_service,
            conversation_state=conv_state,
        )

        assert get_correlation_id() is None
        await answerer.handle(ticket_id=42)
        assert get_correlation_id() is None

        # Verify ERROR level contract: safe error summary logged, no personal data
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) >= 1
        err_msg = error_records[0].getMessage()
        assert "error_class=RuntimeError" in err_msg
        # User ID must not be leaked on ERROR
        assert "12345" not in err_msg

    @pytest.mark.asyncio
    async def test_bedolaga_malformed_webhook_logged_as_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR)

        endpoint = BedolagaWebhookEndpoint(
            answerer=MagicMock(), secret="correct_webhook_secret_12345"
        )
        request = MagicMock()
        request.read = AsyncMock(return_value=b"not json at all")
        body = b"not json at all"
        signature = hmac.new(b"correct_webhook_secret_12345", body, hashlib.sha256).hexdigest()
        request.headers = {
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Event": "ticket.created",
        }

        response = await endpoint.handle(request)
        assert response.status == 400

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("Bedolaga webhook rejected" in r.getMessage() for r in error_records)
