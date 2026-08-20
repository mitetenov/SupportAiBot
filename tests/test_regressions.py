"""Regressions from the Java→Python migration review.

Each test here corresponds to a defect the original suite could not see, because
it exercised components with dependencies wired differently than main() wires
them.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.bot.admin_notifier import AdminNotifier
from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.sender import TelegramMessageSender
from app.llm.mcp_client import HttpMcpClient

MCP_URL = "http://mcp-remnawave:3100"


def mcp_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-1"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "nodes_list",
                                "description": "List nodes",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id", 0), "result": {}})

    return httpx.MockTransport(handle)


class TestMcpWiringMatchesProduction:
    """main() hands the MCP client the application-wide httpx client.

    That client has no base_url and none of the MCP protocol headers, which used
    to make every request fail before it left the process, leaving the model with
    zero tools while the bot started up as if nothing were wrong.
    """

    @pytest.mark.asyncio
    async def test_initializes_with_a_shared_client_that_has_no_base_url(self) -> None:
        seen: list[httpx.Request] = []
        async with httpx.AsyncClient(
            transport=mcp_transport(seen), timeout=httpx.Timeout(30.0)
        ) as shared:
            client = HttpMcpClient(base_url=MCP_URL, http_client=shared)

            assert await client.init() is True
            assert [t.name for t in client.list_tools()] == ["nodes_list"]

    @pytest.mark.asyncio
    async def test_every_request_carries_the_absolute_url_and_protocol_headers(self) -> None:
        seen: list[httpx.Request] = []
        async with httpx.AsyncClient(
            transport=mcp_transport(seen), timeout=httpx.Timeout(30.0)
        ) as shared:
            client = HttpMcpClient(base_url=MCP_URL, http_client=shared)
            await client.init()

        assert seen, "no request was made"
        for request in seen:
            assert str(request.url) == MCP_URL
            assert request.headers["accept"] == "application/json, text/event-stream"

    @pytest.mark.asyncio
    async def test_session_id_is_propagated_after_the_handshake(self) -> None:
        seen: list[httpx.Request] = []
        async with httpx.AsyncClient(
            transport=mcp_transport(seen), timeout=httpx.Timeout(30.0)
        ) as shared:
            await HttpMcpClient(base_url=MCP_URL, http_client=shared).init()

        follow_ups = seen[1:]
        assert follow_ups
        assert all(r.headers.get("mcp-session-id") == "sess-1" for r in follow_ups)


class TestAdminNotificationIsAwaited:
    """notify_error is a coroutine; calling it without awaiting sent nothing."""

    @pytest.mark.asyncio
    async def test_mcp_failure_actually_reaches_the_support_group(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        notifier = AdminNotifier(bot=bot, support_group_chat_id=-100123)

        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as shared:
            client = HttpMcpClient(base_url=MCP_URL, http_client=shared, admin_notifier=notifier)
            assert await client.init() is False

        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.kwargs["text"]
        assert "[ОШИБКА БОТА]" in text
        assert MCP_URL in text

    @pytest.mark.asyncio
    async def test_a_broken_notifier_does_not_mask_the_original_failure(self) -> None:
        notifier = MagicMock()
        notifier.notify_error = AsyncMock(side_effect=RuntimeError("telegram down"))

        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as shared:
            client = HttpMcpClient(base_url=MCP_URL, http_client=shared, admin_notifier=notifier)
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
        monkeypatch.setattr(main_module, "Bot", lambda **_: bot)

        db_manager = MagicMock()
        db_manager.init_models = AsyncMock()
        db_manager.close = AsyncMock()
        monkeypatch.setattr(main_module, "get_db_manager", lambda *_: db_manager)

        faq_initializer = MagicMock()
        faq_initializer.run = AsyncMock()
        monkeypatch.setattr(main_module, "FaqInitializer", lambda **_: faq_initializer)

        knowledge_gaps = MagicMock()
        knowledge_gaps.init_schema = AsyncMock()
        monkeypatch.setattr(main_module, "KnowledgeGapService", lambda **_: knowledge_gaps)

        mcp_client = MagicMock()
        mcp_client.init = AsyncMock(return_value=True)
        mcp_client.close = AsyncMock()
        mcp_client.list_tools = MagicMock(return_value=[])
        monkeypatch.setattr(main_module, "HttpMcpClient", lambda **_: mcp_client)

        health_runner = MagicMock()
        monkeypatch.setattr(
            main_module, "start_health_server", AsyncMock(return_value=health_runner)
        )
        monkeypatch.setattr(main_module, "stop_health_server", AsyncMock())

        reached_polling = asyncio.Event()

        class StubDispatcher:
            def include_router(self, _router: object) -> None:
                self.router = _router

            def resolve_used_update_types(self) -> list[str]:
                return ["message", "message_reaction"]

            async def start_polling(self, *_args: object, **_kwargs: object) -> None:
                reached_polling.set()

        monkeypatch.setattr(main_module, "Dispatcher", StubDispatcher)

        await main_module.main()

        assert reached_polling.is_set(), "main() never got to long-polling"
        mcp_client.init.assert_awaited_once()
        bot.set_my_commands.assert_awaited_once()
        db_manager.init_models.assert_awaited_once()
        faq_initializer.run.assert_awaited_once()
        # ...and it tore everything back down.
        mcp_client.close.assert_awaited_once()
        db_manager.close.assert_awaited_once()
