"""Application entrypoint, dependency injection composition root, and HTTP healthcheck server."""

import asyncio
import logging
from datetime import timedelta

import httpx
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiohttp import web

from app.bedolaga import TicketSupport, create_ticket_support
from app.bot.admin_notifier import AdminNotifier
from app.bot.buffer import UserMessageBuffer
from app.bot.command_handler import SupportCommandHandler
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.maintenance import MaintenanceScheduler, build_default_jobs
from app.bot.operator_ask import OperatorAskCommand
from app.bot.photo_downloader import PhotoDownloader
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.router import setup_router
from app.bot.sender import TelegramMessageSender
from app.bot.topic_manager import TopicManager
from app.bot.typing import TypingIndicator
from app.config import get_settings, reveal
from app.llm import create_llm_client
from app.llm.mcp_client import HttpMcpClient
from app.llm.mcp_router import McpRouter
from app.rag.embedding import create_embedding_provider
from app.rag.initializer import FaqInitializer
from app.rag.knowledge_gaps import KnowledgeGapService
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService
from app.storage.database import get_db_manager
from app.storage.schema import sync_legacy_schema

logger = logging.getLogger(__name__)

#: How long shutdown waits for Bedolaga ticket turns already in flight.
#: `docker compose` allows the whole shutdown ten seconds by default, and the
#: Telegram buffer drain has to fit in there too, so this is a courtesy for the
#: turn that is nearly done — not a promise to finish one that just started.
TICKET_DRAIN_TIMEOUT_SECONDS: float = 3.0

__all__ = [
    "create_health_app",
    "create_llm_client",
    "health_handler",
    "main",
    "register_bot_commands",
    "start_health_server",
    "stop_health_server",
]


async def health_handler(_request: web.Request) -> web.Response:
    """Return JSON health status."""
    return web.json_response({"status": "UP"})


def create_health_app() -> web.Application:
    """Create lightweight aiohttp application for Docker / Kubernetes healthchecks."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/actuator/health", health_handler)
    return app


async def start_health_server(
    app: web.Application,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> web.AppRunner:
    """Start background aiohttp healthcheck server."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Healthcheck HTTP server started on %s:%d", host, port)
    return runner


async def stop_health_server(runner: web.AppRunner) -> None:
    """Stop and cleanup background aiohttp healthcheck server."""
    logger.info("Stopping healthcheck HTTP server...")
    await runner.cleanup()


async def register_bot_commands(bot: Bot) -> None:
    """Register bot slash commands menu in Telegram."""
    commands = [
        BotCommand(command="start", description="Начать заново, сбросить историю"),
        BotCommand(command="operator", description="Связаться с живым оператором"),
        BotCommand(command="help", description="Что умеет бот"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands menu registered successfully")


async def main() -> None:
    """Application async entrypoint and composition root."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting VPN Support Bot...")

    settings = get_settings()
    logger.info(
        "Loaded settings: LLM provider=%s, Embedding provider=%s, Group ID=%d",
        settings.llm_provider,
        settings.embedding_provider,
        settings.telegram_support_group_chat_id,
    )

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    bot = Bot(token=reveal(settings.telegram_bot_token))
    sender = TelegramMessageSender(bot)
    db_manager = get_db_manager(settings.database_url)

    health_runner: web.AppRunner | None = None
    mcp_client: HttpMcpClient | None = None
    message_buffer: UserMessageBuffer | None = None
    pipeline: UserMessagePipeline | None = None
    typing_indicator: TypingIndicator | None = None
    maintenance: MaintenanceScheduler | None = None
    ticket_support: TicketSupport | None = None

    try:
        # 1. Initialize DB models, then reconcile anything an earlier version left behind
        await db_manager.init_models()
        await sync_legacy_schema(db_manager.engine)

        # 2. Setup Embedding and RAG services
        embedding_provider = create_embedding_provider(settings, client=http_client)
        faq_service = FaqEmbeddingService(
            db_manager=db_manager,
            embedding_provider=embedding_provider,
        )
        knowledge_gap_service = KnowledgeGapService(
            db_manager=db_manager,
            faq_service=faq_service,
            embedding_provider=embedding_provider,
        )
        faq_initializer = FaqInitializer(service=faq_service)

        # 3. Setup MCP client & router
        admin_notifier = AdminNotifier(
            bot=bot,
            support_group_chat_id=settings.telegram_support_group_chat_id,
        )
        mcp_client = HttpMcpClient(
            base_url=settings.remnawave_mcp_url,
            http_client=http_client,
            settings=settings,
            admin_notifier=admin_notifier,
        )
        await mcp_client.init()
        mcp_router = McpRouter(
            clients=[mcp_client],
            readonly=settings.remnawave_mcp_readonly,
            settings=settings,
        )
        if not mcp_router.list_tools():
            # mcp-remnawave keeps a single global session and offers no way to
            # release it, so a bot restarted on its own gets "already
            # initialized" and comes up with no Remnawave tools at all. That is
            # invisible to users — the model just answers without their data —
            # so say it out loud rather than leaving one WARNING in the log.
            logger.error(
                "Starting WITHOUT Remnawave tools — the model cannot look up "
                "subscriptions, nodes or devices. Restart %s to clear its session.",
                settings.remnawave_mcp_url,
            )
            await admin_notifier.notify_error(
                "Бот запущен БЕЗ инструментов Remnawave: MCP-сервер держит "
                f"занятую сессию ({settings.remnawave_mcp_url}). "
                "Перезапустите контейнер mcp-remnawave вместе с ботом.",
                error=RuntimeError("no MCP tools exposed"),
            )

        # 4. Setup Chat History & LLM Client
        chat_history_service = ChatHistoryService(
            db_manager=db_manager,
            max_messages=settings.chat_history_max_messages,
            ttl_days=settings.chat_history_ttl_days,
        )
        llm_client = create_llm_client(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )

        # 5. Setup Bot Pipeline & Router
        topic_manager = TopicManager(
            db_manager=db_manager,
            bot=bot,
            support_group_chat_id=settings.telegram_support_group_chat_id,
        )
        forwarder = SupportGroupForwarder(
            sender=sender,
            topic_manager=topic_manager,
            db_manager=db_manager,
            support_group_chat_id=settings.telegram_support_group_chat_id,
            admin_username=settings.telegram_support_admin_username,
        )
        rate_limiter = UserRateLimiter()
        conversation_state = ConversationState(
            operator_suppression_window=timedelta(
                minutes=settings.conversation_operator_suppression_window_minutes
            ),
            last_query_ttl=timedelta(hours=settings.conversation_last_query_ttl_hours),
        )
        typing_indicator = TypingIndicator(bot=bot)
        pipeline = UserMessagePipeline(
            llm_client=llm_client,
            sender=sender,
            forwarder=forwarder,
            rate_limiter=rate_limiter,
            knowledge_gap_service=knowledge_gap_service,
            conversation_state=conversation_state,
            typing_indicator=typing_indicator,
        )
        operator_ask = OperatorAskCommand(
            llm_client=llm_client,
            sender=sender,
            conversation_state=conversation_state,
            typing_indicator=typing_indicator,
            support_group_chat_id=settings.telegram_support_group_chat_id,
        )
        command_handler = SupportCommandHandler(
            sender=sender,
            db_manager=db_manager,
            knowledge_gap_service=knowledge_gap_service,
            admin_telegram_ids=settings.telegram_support_admin_telegram_ids,
        )
        photo_downloader = PhotoDownloader(bot=bot, http_client=http_client)
        message_buffer = UserMessageBuffer(
            window_ms=settings.telegram_buffer_window_ms,
            max_messages=settings.telegram_buffer_max_messages,
        )
        ticket_support = create_ticket_support(
            settings=settings,
            http_client=http_client,
            llm_client=llm_client,
            db_manager=db_manager,
            forwarder=forwarder,
            admin_notifier=admin_notifier,
            rate_limiter=rate_limiter,
            knowledge_gap_service=knowledge_gap_service,
            conversation_state=conversation_state,
        )

        router = setup_router(
            sender=sender,
            llm_client=llm_client,
            forwarder=forwarder,
            db_manager=db_manager,
            chat_history_service=chat_history_service,
            knowledge_gap_service=knowledge_gap_service,
            command_handler=command_handler,
            photo_downloader=photo_downloader,
            message_buffer=message_buffer,
            pipeline=pipeline,
            conversation_state=conversation_state,
            operator_ask=operator_ask,
            support_group_chat_id=settings.telegram_support_group_chat_id,
        )

        dp = Dispatcher()
        dp.include_router(router)

        # 6. Initialize FAQ data and Knowledge gaps schema
        await faq_initializer.run()
        await knowledge_gap_service.init_schema()

        # 7. Start recurring cleanups (chat history TTL, rate limiter, conversation state)
        jobs = build_default_jobs(chat_history_service, rate_limiter, conversation_state)
        if ticket_support is not None:
            jobs.append(ticket_support.maintenance_job())
        maintenance = MaintenanceScheduler(jobs)
        maintenance.start()

        # 8. Start Healthcheck server
        health_app = create_health_app()
        if ticket_support is not None:
            ticket_support.register_routes(health_app)
        health_runner = await start_health_server(
            health_app,
            port=settings.healthcheck_port,
        )

        # 9. Register bot commands in Telegram
        await register_bot_commands(bot)

        # 10. Start long-polling
        logger.info("Starting Telegram bot long-polling...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    finally:
        # aiogram installs its own SIGINT/SIGTERM handlers in start_polling, so
        # `docker stop` unwinds through here rather than killing the process.
        logger.info("Shutting down VPN Support Bot...")
        if maintenance is not None:
            await maintenance.stop()
        if health_runner is not None:
            # Before either drain: while this is up, a Bedolaga webhook
            # delivery still schedules new background work, and nothing below
            # would wait for a task created after the drain it belongs to.
            await stop_health_server(health_runner)
        if message_buffer is not None:
            # Anything still buffered or being answered gets its turn first;
            # only then do the clients it needs get closed underneath it.
            # Telegram goes before tickets: it is the larger audience and the
            # one that worked before the integration existed.
            if pipeline is not None:
                await message_buffer.drain(pipeline.handle)
            message_buffer.shutdown()
        if ticket_support is not None:
            # A ticket half-answered on shutdown is a user waiting forever:
            # the model call already cost tokens, and nothing would retry it.
            # Bounded, because one turn is a model call plus retries plus up to
            # five tool-loop iterations, and `docker stop` gives the whole
            # sequence ten seconds before SIGKILL — an unbounded wait here
            # would take everything after it down with it.
            try:
                await asyncio.wait_for(
                    ticket_support.answerer.drain(),
                    timeout=TICKET_DRAIN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Bedolaga ticket turns did not finish within %.0fs; "
                    "the next sweep after restart picks them up",
                    TICKET_DRAIN_TIMEOUT_SECONDS,
                )
        if typing_indicator is not None:
            typing_indicator.shutdown()
        if mcp_client is not None:
            await mcp_client.close()
        await http_client.aclose()
        if bot.session:
            await bot.session.close()
        await db_manager.close()
        logger.info("Shutdown completed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
