"""Regressions from the Java→Python migration review.

Each test here corresponds to a defect the original suite could not see, because
it exercised components with dependencies wired differently than main() wires
them.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.admin_notifier import AdminNotifier
from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.sender import TelegramMessageSender
from app.llm.mcp_client import HttpMcpClient, McpTool

MCP_URL = "http://mcp-remnawave:3100"


class TestMcpWiringMatchesProduction:
    """main() constructs HttpMcpClient with server_name, base_url and admin_notifier.

    The client uses the official SDK v2 Client and initializes independently.
    """

    @pytest.mark.asyncio
    async def test_initializes_with_sdk_client_factory(self) -> None:
        mock_tool = MagicMock()
        mock_tool.name = "nodes_list"
        mock_tool.description = "List nodes"
        mock_tool.input_schema = {"type": "object"}

        class FakeSdkClient:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False

            async def __aenter__(self) -> FakeSdkClient:
                return self

            async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                self.closed = True

            def protocol_version(self) -> str:
                return "2026-07-28"

            async def list_tools(self, cursor: str | None = None) -> Any:
                res = MagicMock()
                res.tools = [mock_tool]
                res.next_cursor = None
                return res

        client = HttpMcpClient(
            server_name="remnawave",
            base_url=MCP_URL,
            client_factory=lambda url: FakeSdkClient(url),
        )

        assert await client.init() is True
        assert [t.name for t in client.list_tools()] == ["nodes_list"]
        assert client.protocol_version == "2026-07-28"
        await client.close()


class TestAdminNotificationIsAwaited:
    """notify_error is a coroutine; calling it without awaiting sent nothing."""

    @pytest.mark.asyncio
    async def test_mcp_failure_actually_reaches_the_support_group(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        notifier = AdminNotifier(bot=bot, support_group_chat_id=-100123)

        class ExplodingSdkClient:
            def __init__(self, _url: str) -> None:
                pass

            async def __aenter__(self) -> ExplodingSdkClient:
                raise ConnectionRefusedError("connection refused")

            async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                pass

        client = HttpMcpClient(
            server_name="remnawave",
            base_url=MCP_URL,
            admin_notifier=notifier,
            client_factory=lambda url: ExplodingSdkClient(url),
        )
        assert await client.init() is False

        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.kwargs["text"]
        assert "[ОШИБКА БОТА]" in text
        assert MCP_URL in text

    @pytest.mark.asyncio
    async def test_a_broken_notifier_does_not_mask_the_original_failure(self) -> None:
        notifier = MagicMock()
        notifier.notify_error = AsyncMock(side_effect=RuntimeError("telegram down"))

        class ExplodingSdkClient:
            def __init__(self, _url: str) -> None:
                pass

            async def __aenter__(self) -> ExplodingSdkClient:
                raise ConnectionRefusedError("connection refused")

            async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                pass

        client = HttpMcpClient(
            server_name="remnawave",
            base_url=MCP_URL,
            admin_notifier=notifier,
            client_factory=lambda url: ExplodingSdkClient(url),
        )
        assert await client.init() is False


class TestUserReplyFailureStillReachesSupport:
    """A send that fails must not also cancel the forward to the support topic."""

    @pytest.mark.asyncio
    async def test_blocked_user_does_not_swallow_the_forward(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked by the user"))

        forwarder = MagicMock()
        forwarder.forward_to_support = AsyncMock()
        forwarder.forward_error_to_topic = AsyncMock()

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(text="ответ", faq_context=None))

        gaps = MagicMock()
        gaps.evaluate = AsyncMock()

        pipeline = UserMessagePipeline(
            llm_client=llm,
            sender=TelegramMessageSender(bot),
            forwarder=forwarder,
            rate_limiter=UserRateLimiter(),
            knowledge_gap_service=gaps,
            conversation_state=ConversationState(),
            typing_indicator=MagicMock(),
        )

        message = MagicMock()
        message.chat.id = 7
        batch = MessageBatch(
            last_message=message, user=MagicMock(id=7), text="привет", message_ids=[1]
        )

        await pipeline.handle(batch)

        forwarder.forward_to_support.assert_awaited_once()
        forwarder.forward_error_to_topic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_answer_over_the_telegram_limit_is_delivered_in_full(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()

        long_answer = "\n".join(f"шаг {i}: " + "и" * 100 for i in range(80))

        forwarder = MagicMock()
        forwarder.forward_to_support = AsyncMock()
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(text=long_answer, faq_context=None))
        gaps = MagicMock()
        gaps.evaluate = AsyncMock()

        pipeline = UserMessagePipeline(
            llm_client=llm,
            sender=TelegramMessageSender(bot),
            forwarder=forwarder,
            rate_limiter=UserRateLimiter(),
            knowledge_gap_service=gaps,
            conversation_state=ConversationState(),
            typing_indicator=MagicMock(),
        )

        message = MagicMock()
        message.chat.id = 7
        batch = MessageBatch(
            last_message=message, user=MagicMock(id=7), text="как настроить", message_ids=[1]
        )

        await pipeline.handle(batch)

        sent = [c.kwargs["text"] for c in bot.send_message.await_args_list]
        assert len(sent) > 1, "long answer was not split"
        assert all(len(chunk) <= 4096 for chunk in sent)
        assert "\n".join(sent) == long_answer


def _stub_process_boundaries(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Replace everything main() talks to outside this process.

    Telegram, Postgres, the MCP server and long-polling — and nothing else, so
    the real composition root is what runs.
    """
    import app.main as main_module
    from app.config import Settings

    settings = Settings(
        telegram_bot_token="123:ABC",
        telegram_support_group_chat_id=-1001234567890,
        telegram_support_admin_username="admin",
        telegram_support_admin_telegram_ids={1},
        llm_provider="deepseek",
        deepseek_api_key="sk-deepseek",
        deepseek_model="deepseek-chat",
        embedding_provider="gemini",
        gemini_api_key="gemini-key",
        remnawave_mcp_url=MCP_URL,
        healthcheck_port=0,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    bot = MagicMock()
    bot.session = None
    bot.set_my_commands = AsyncMock()
    bot.send_message = AsyncMock()
    monkeypatch.setattr(main_module, "Bot", lambda **_: bot)

    db_manager = MagicMock()
    db_manager.init_models = AsyncMock()
    db_manager.close = AsyncMock()
    monkeypatch.setattr(main_module, "get_db_manager", lambda *_: db_manager)
    # Schema reconciliation is integration-tested against a real engine; this
    # composition-root test deliberately replaces every process boundary, so
    # do not let an AsyncMock engine manufacture unawaited-result warnings.
    monkeypatch.setattr(main_module, "sync_legacy_schema", AsyncMock(return_value=[]))

    faq_initializer = MagicMock()
    faq_initializer.run = AsyncMock()
    monkeypatch.setattr(main_module, "FaqInitializer", lambda **_: faq_initializer)

    knowledge_gaps = MagicMock()
    knowledge_gaps.init_schema = AsyncMock()
    monkeypatch.setattr(main_module, "KnowledgeGapService", lambda **_: knowledge_gaps)

    mcp_client = MagicMock()
    mcp_client.base_url = MCP_URL
    mcp_client.init = AsyncMock(return_value=True)
    mcp_client.close = AsyncMock()
    mcp_client.list_tools = MagicMock(
        return_value=[McpTool(name="nodes_list", description="List nodes")]
    )
    monkeypatch.setattr(main_module, "HttpMcpClient", lambda **_: mcp_client)

    health_runner = MagicMock()
    stop_health_server = AsyncMock()
    monkeypatch.setattr(main_module, "start_health_server", AsyncMock(return_value=health_runner))
    monkeypatch.setattr(main_module, "stop_health_server", stop_health_server)

    reached_polling = asyncio.Event()

    class StubDispatcher:
        def include_router(self, _router: object) -> None:
            self.router = _router

        def resolve_used_update_types(self) -> list[str]:
            return ["message", "message_reaction"]

        async def start_polling(self, *_args: object, **_kwargs: object) -> None:
            reached_polling.set()

    monkeypatch.setattr(main_module, "Dispatcher", StubDispatcher)

    return {
        "bot": bot,
        "db_manager": db_manager,
        "faq_initializer": faq_initializer,
        "mcp_client": mcp_client,
        "reached_polling": reached_polling,  # type: ignore[dict-item]
        "stop_health_server": stop_health_server,
    }


class TestCompositionRoot:
    """main() is where the MCP bug lived, and nothing exercised main().

    This drives the real composition root with only the process boundaries
    (Telegram, Postgres, long-polling) stubbed, so a constructor whose signature
    drifts away from its call site fails here instead of in production.
    """

    @pytest.mark.asyncio
    async def test_builds_the_whole_graph_and_reaches_long_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module

        parts = _stub_process_boundaries(monkeypatch)

        await main_module.main()

        assert parts["reached_polling"].is_set(), "main() never got to long-polling"
        parts["mcp_client"].init.assert_awaited_once()
        parts["bot"].set_my_commands.assert_awaited_once()
        parts["db_manager"].init_models.assert_awaited_once()
        parts["faq_initializer"].run.assert_awaited_once()
        # ...and it tore everything back down.
        parts["mcp_client"].close.assert_awaited_once()
        parts["db_manager"].close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reports_a_handshake_failure_without_claiming_the_session_is_occupied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module

        parts = _stub_process_boundaries(monkeypatch)
        parts["mcp_client"].init.return_value = False
        parts["mcp_client"].list_tools.return_value = []

        await main_module.main()

        text = parts["bot"].send_message.await_args.kwargs["text"]
        assert "не удалось инициализировать MCP" in text
        assert "занятую сессию" not in text
        assert MCP_URL in text

    @pytest.mark.asyncio
    async def test_reports_an_empty_allowed_tool_set_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module

        parts = _stub_process_boundaries(monkeypatch)
        parts["mcp_client"].init.return_value = True
        parts["mcp_client"].list_tools.return_value = []

        await main_module.main()

        text = parts["bot"].send_message.await_args.kwargs["text"]
        assert "MCP вернул 0 разрешённых инструментов" in text
        assert "MCP_TAG" in text
        assert "REMNAWAVE_IS_SUPPORT" in text


class TestShutdownOrder:
    """`docker stop` gives the whole sequence ten seconds before SIGKILL.

    A ticket turn is a model call plus retries plus a tool loop, so waiting on
    one without a bound used to put the Telegram buffer drain — the path that
    worked long before this integration existed — behind an unbounded wait.
    """

    @pytest.mark.asyncio
    async def test_a_stuck_ticket_drain_cannot_hold_up_the_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module

        parts = _stub_process_boundaries(monkeypatch)
        monkeypatch.setattr(main_module, "TICKET_DRAIN_TIMEOUT_SECONDS", 0.01)

        order: list[str] = []
        never_finishes = asyncio.Event()

        async def stuck_drain() -> None:
            order.append("ticket-drain")
            await never_finishes.wait()

        async def stop_health(_runner: object) -> None:
            order.append("stop-health")

        monkeypatch.setattr(main_module, "stop_health_server", stop_health)

        from app.bot.buffer import UserMessageBuffer

        buffer_drain = UserMessageBuffer.drain

        async def recording_drain(
            self: UserMessageBuffer, sink: object, timeout: float = 20.0
        ) -> None:
            order.append("telegram-drain")
            await buffer_drain(self, sink, timeout)  # type: ignore[arg-type]

        monkeypatch.setattr(UserMessageBuffer, "drain", recording_drain)

        from app.bot.maintenance import MaintenanceJob

        ticket_support = MagicMock()
        ticket_support.answerer.drain = stuck_drain
        ticket_support.register_routes = MagicMock()
        ticket_support.maintenance_job = MagicMock(
            return_value=MaintenanceJob(
                name="bedolaga-ticket-sweep",
                interval_seconds=3600.0,
                run=AsyncMock(return_value=0),
            )
        )
        monkeypatch.setattr(main_module, "create_ticket_support", lambda **_: ticket_support)

        await main_module.main()

        # The shutdown got past the stuck drain and closed everything after it.
        parts["db_manager"].close.assert_awaited_once()
        parts["mcp_client"].close.assert_awaited_once()
        # The webhook endpoint went down first, so no delivery could schedule
        # work nothing would wait for; then Telegram — the larger audience and
        # the older path — got its turn before the optional integration did.
        assert order == ["stop-health", "telegram-drain", "ticket-drain"]
