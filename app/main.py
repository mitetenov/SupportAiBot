"""Application entrypoint, dependency injection composition root, and HTTP healthcheck server."""

import asyncio
import logging
from datetime import timedelta

import httpx
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiohttp import web

from app.bot.admin_notifier import AdminNotifier
from app.bot.buffer import UserMessageBuffer
from app.bot.command_handler import SupportCommandHandler
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.photo_downloader import PhotoDownloader
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.router import setup_router
from app.bot.topic_manager import TopicManager
from app.bot.typing import TypingIndicator
from app.config import Settings, get_settings
from app.llm.base import LlmClient
from app.llm.deepseek import DeepSeekClient
from app.llm.gemini import GeminiClient
from app.llm.mcp_client import HttpMcpClient
from app.llm.mcp_router import McpRouter
from app.llm.openai_client import OpenAiClient
from app.rag.embedding import create_embedding_provider
from app.rag.initializer import FaqInitializer
from app.rag.knowledge_gaps import KnowledgeGapService
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService
from app.storage.database import DatabaseSessionManager, get_db_manager

logger = logging.getLogger(__name__)


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
        BotCommand(command="start", description="Начать общение с ботом"),
        BotCommand(command="operator", description="Позвать оператора поддержки"),
        BotCommand(command="help", description="Помощь и часто задаваемые вопросы"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands menu registered successfully")


def create_llm_client(
    settings: Settings,
    mcp_router: McpRouter,
    chat_history_service: ChatHistoryService,
    faq_service: FaqEmbeddingService,
    db_manager: DatabaseSessionManager,
    http_client: httpx.AsyncClient,
) -> LlmClient:
    """Instantiate configured LLM client implementation."""
    provider = settings.llm_provider.strip().lower()
    if provider == "deepseek":
        return DeepSeekClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    elif provider == "gemini":
        return GeminiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    elif provider == "openai":
        return OpenAiClient(
            settings=settings,
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_service,
            db_manager=db_manager,
            http_client=http_client,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")


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
    bot = Bot(token=settings.telegram_bot_token)
    db_manager = get_db_manager(settings.database_url)

    health_runner: web.AppRunner | None = None
    mcp_client: HttpMcpClient | None = None
    message_buffer: UserMessageBuffer | None = None
    typing_indicator: TypingIndicator | None = None

    try:
        # 1. Initialize DB models
        await db_manager.init_models()

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
            bot=bot,
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
            bot=bot,
            forwarder=forwarder,
            rate_limiter=rate_limiter,
            knowledge_gap_service=knowledge_gap_service,
            conversation_state=conversation_state,
            typing_indicator=typing_indicator,
        )
        command_handler = SupportCommandHandler(
            bot=bot,
            db_manager=db_manager,
            knowledge_gap_service=knowledge_gap_service,
            admin_telegram_ids=settings.telegram_support_admin_telegram_ids,
        )
        photo_downloader = PhotoDownloader(bot=bot, http_client=http_client)
        message_buffer = UserMessageBuffer(
            window_ms=settings.telegram_buffer_window_ms,
            max_messages=settings.telegram_buffer_max_messages,
        )

        router = setup_router(
            bot=bot,
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
            support_group_chat_id=settings.telegram_support_group_chat_id,
        )

        dp = Dispatcher()
        dp.include_router(router)

        # 6. Initialize FAQ data and Knowledge gaps schema
        await faq_initializer.run()
        await knowledge_gap_service.init_schema()

        # 7. Start Healthcheck server
        health_app = create_health_app()
        health_runner = await start_health_server(
            health_app,
            port=settings.healthcheck_port,
        )

        # 8. Register bot commands in Telegram
        await register_bot_commands(bot)

        # 9. Start long-polling
        logger.info("Starting Telegram bot long-polling...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    finally:
        logger.info("Shutting down VPN Support Bot...")
        if message_buffer is not None:
            message_buffer.shutdown()
        if typing_indicator is not None:
            typing_indicator.shutdown()
        if health_runner is not None:
            await stop_health_server(health_runner)
        if mcp_client is not None:
            await mcp_client.close()
        await http_client.aclose()
        if bot.session:
            await bot.session.close()
        await db_manager.close()
        logger.info("Shutdown completed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
