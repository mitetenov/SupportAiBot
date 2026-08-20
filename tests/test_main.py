"""Tests for main entrypoint, healthcheck server, and dependency injection."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from app.config import Settings
from app.main import (
    create_health_app,
    create_llm_client,
    health_handler,
    register_bot_commands,
    start_health_server,
    stop_health_server,
)


@pytest.fixture
def mock_settings() -> Settings:
    """Create a mock settings instance for testing."""
    return Settings(
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_support_group_chat_id=-1001234567890,
        telegram_support_admin_username="admin_user",
        telegram_support_admin_telegram_ids={123456, 789012},
        llm_provider="deepseek",
        embedding_provider="gemini",
        deepseek_api_key="sk-test-deepseek-key",
        deepseek_model="deepseek-chat",
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-2.5-flash",
        openai_api_key="sk-test-openai-key",
        openai_model="gpt-5.6-luna",
        remnawave_mcp_url="http://localhost:3100",
        healthcheck_port=8080,
    )


@pytest.mark.asyncio
async def test_healthcheck_handler() -> None:
    """Test that health_handler returns 200 OK and {'status': 'UP'}."""
    req = make_mocked_request("GET", "/health")
    resp = await health_handler(req)
    assert resp.status == 200
    assert resp.content_type == "application/json"
    body = json.loads(resp.text)
    assert body == {"status": "UP"}


def test_create_health_app_routes() -> None:
    """Test that create_health_app registers /health and /actuator/health."""
    app = create_health_app()
    routes = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/health" in routes
    assert "/actuator/health" in routes


@pytest.mark.asyncio
async def test_start_and_stop_health_server() -> None:
    """Test start_health_server and stop_health_server lifecycle."""
    app = create_health_app()
    with patch("aiohttp.web.TCPSite.start", new_callable=AsyncMock) as mock_site_start:
        runner = await start_health_server(app, host="127.0.0.1", port=8080)
        assert runner is not None
        mock_site_start.assert_called_once()
        await stop_health_server(runner)


@pytest.mark.asyncio
async def test_register_bot_commands() -> None:
    """Test registering bot commands menu."""
    bot = AsyncMock()
    await register_bot_commands(bot)
    bot.set_my_commands.assert_called_once()
    commands = bot.set_my_commands.call_args[0][0]
    cmd_names = [c.command for c in commands]
    assert "start" in cmd_names
    assert "operator" in cmd_names
    assert "help" in cmd_names


def test_create_llm_client(mock_settings: Settings) -> None:
    """Test creating LLM client based on provider setting."""
    mcp_router = MagicMock()
    chat_history = MagicMock()
    faq_service = MagicMock()
    db_manager = MagicMock()
    http_client = MagicMock()

    # DeepSeek
    mock_settings.llm_provider = "deepseek"
    client_ds = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert client_ds.get_provider_name() == "DeepSeek"

    # Gemini
    mock_settings.llm_provider = "gemini"
    client_gemini = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert client_gemini.get_provider_name() == "Gemini"

    # OpenAI
    mock_settings.llm_provider = "openai"
    client_openai = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert client_openai.get_provider_name() == "OpenAI"

    # Invalid provider
    mock_settings.llm_provider = "unsupported"
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        create_llm_client(
            mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
        )


@pytest.mark.asyncio
async def test_main_lifecycle(mock_settings: Settings) -> None:
    """Test the full main lifecycle with mocked external services."""
    from app.main import main

    with (
        patch("app.main.get_settings", return_value=mock_settings),
        patch("app.main.get_db_manager") as mock_get_db,
        patch("app.main.HttpMcpClient") as mock_mcp_client_cls,
        patch("app.main.FaqInitializer") as mock_faq_init_cls,
        patch("app.main.KnowledgeGapService") as mock_gap_service_cls,
        patch("app.main.Dispatcher") as mock_dispatcher_cls,
        patch("app.main.Bot") as mock_bot_cls,
        patch("app.main.start_health_server", new_callable=AsyncMock) as mock_start_health,
        patch("app.main.stop_health_server", new_callable=AsyncMock) as mock_stop_health,
    ):
        mock_db = MagicMock()
        mock_db.init_models = AsyncMock()
        mock_db.close = AsyncMock()
        mock_get_db.return_value = mock_db

        mock_mcp = MagicMock()
        mock_mcp.init = AsyncMock(return_value=True)
        mock_mcp.close = AsyncMock()
        mock_mcp.list_tools = MagicMock(return_value=[])
        mock_mcp_client_cls.return_value = mock_mcp

        mock_faq_init = MagicMock()
        mock_faq_init.run = AsyncMock()
        mock_faq_init_cls.return_value = mock_faq_init

        mock_gap_service = MagicMock()
        mock_gap_service.init_schema = AsyncMock()
        mock_gap_service_cls.return_value = mock_gap_service

        mock_bot = MagicMock()
        mock_bot.session.close = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        mock_dp = MagicMock()
        mock_dp.start_polling = AsyncMock()
        mock_dp.resolve_used_update_types = MagicMock(return_value=["message", "message_reaction"])
        mock_dispatcher_cls.return_value = mock_dp

        mock_runner = MagicMock()
        mock_start_health.return_value = mock_runner

        await main()

        mock_db.init_models.assert_called_once()
        mock_mcp.init.assert_called_once()
        mock_faq_init.run.assert_called_once()
        mock_gap_service.init_schema.assert_called_once()
        mock_start_health.assert_called_once()
        mock_dp.start_polling.assert_called_once()

        # Check teardown
        mock_stop_health.assert_called_once_with(mock_runner)
        mock_mcp.close.assert_called_once()
        mock_bot.session.close.assert_called_once()
        mock_db.close.assert_called_once()
