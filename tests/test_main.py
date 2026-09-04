"""Tests for main entrypoint, healthcheck server, and dependency injection."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from app.config import Settings
from app.llm.mcp_client import McpTool
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

    # OpenRouter
    mock_settings.llm_provider = "openrouter"
    mock_settings.openrouter_api_key = "test-openrouter-key"  # type: ignore[assignment]
    mock_settings.openrouter_model = "z-ai/glm-4.7"
    client_openrouter = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert client_openrouter.get_provider_name() == "OpenRouter"

    # Z.AI
    mock_settings.llm_provider = "zai"
    mock_settings.zai_api_key = "test-zai-key"  # type: ignore[assignment]
    mock_settings.zai_model = "glm-4.7"
    client_zai = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert client_zai.get_provider_name() == "Z.AI"

    # Invalid provider
    mock_settings.llm_provider = "unsupported"
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        create_llm_client(
            mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
        )


def test_create_llm_client_openrouter_factory(mock_settings: Settings) -> None:
    from app.config import LlmProviderTarget
    from app.llm.fallback import LlmFallbackClient
    from app.llm.openrouter import OpenRouterClient

    mcp_router = MagicMock()
    chat_history = MagicMock()
    faq_service = MagicMock()
    db_manager = MagicMock()
    http_client = MagicMock()

    mock_settings.llm_provider = "openrouter"
    mock_settings.openrouter_api_key = "test-openrouter-key"  # type: ignore[assignment]
    mock_settings.openrouter_model = "z-ai/glm-4.7"

    primary_client = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert isinstance(primary_client, OpenRouterClient)
    assert not isinstance(primary_client, LlmFallbackClient)
    assert primary_client.model == "z-ai/glm-4.7"
    assert primary_client.get_provider_name() == "OpenRouter"

    fallback_settings = mock_settings.model_copy(
        update={
            "llm_provider": "openrouter",
            "openrouter_model": "z-ai/glm-4.7",
            "llm_fallback_chain": (LlmProviderTarget(provider="openrouter", model="z-ai/glm-5.3"),),
            "reasoning_effort": "low",
        }
    )
    fallback_coord = create_llm_client(
        fallback_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert isinstance(fallback_coord, LlmFallbackClient)
    assert len(fallback_coord._clients) == 2
    c1, c2 = fallback_coord._clients
    assert isinstance(c1, OpenRouterClient)
    assert isinstance(c2, OpenRouterClient)
    assert c1.model == "z-ai/glm-4.7"
    assert c2.model == "z-ai/glm-5.3"
    assert c1._http_client is http_client
    assert c2._http_client is http_client
    assert fallback_settings.openrouter_model == "z-ai/glm-4.7"


def test_create_llm_client_zai_factory(mock_settings: Settings) -> None:
    from app.config import LlmProviderTarget
    from app.llm.fallback import LlmFallbackClient
    from app.llm.zai import ZaiClient

    mcp_router = MagicMock()
    chat_history = MagicMock()
    faq_service = MagicMock()
    db_manager = MagicMock()
    http_client = MagicMock()

    mock_settings.llm_provider = "zai"
    mock_settings.zai_api_key = "test-zai-key"  # type: ignore[assignment]
    mock_settings.zai_model = "glm-4.7"

    primary_client = create_llm_client(
        mock_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert isinstance(primary_client, ZaiClient)
    assert not isinstance(primary_client, LlmFallbackClient)
    assert primary_client.model == "glm-4.7"
    assert primary_client.get_provider_name() == "Z.AI"

    fallback_settings = mock_settings.model_copy(
        update={
            "llm_provider": "zai",
            "zai_model": "glm-4.7",
            "llm_fallback_chain": (LlmProviderTarget(provider="zai", model="glm-5.3"),),
            "reasoning_effort": "low",
        }
    )
    fallback_coord = create_llm_client(
        fallback_settings, mcp_router, chat_history, faq_service, db_manager, http_client
    )
    assert isinstance(fallback_coord, LlmFallbackClient)
    assert len(fallback_coord._clients) == 2
    c1, c2 = fallback_coord._clients
    assert isinstance(c1, ZaiClient)
    assert isinstance(c2, ZaiClient)
    assert c1.model == "glm-4.7"
    assert c2.model == "glm-5.3"
    assert c1._http_client is http_client
    assert c2._http_client is http_client
    assert fallback_settings.zai_model == "glm-4.7"


@pytest.mark.parametrize(
    ("fallback_chain", "expected_err_substring"),
    [
        ("openrouter:z-ai/glm-5.3", "z-ai/glm-5.3"),
        ("zai:glm-5.3", "glm-5.3"),
    ],
)
def test_create_llm_client_fallback_rejects_glm_5_3_with_effort_none(
    mock_settings: Settings,
    fallback_chain: str,
    expected_err_substring: str,
) -> None:
    """Verify create_llm_client raises ValueError before HTTP when fallback has glm-5.3 with effort none."""
    from app.config import _parse_fallback_chain

    mcp_router = MagicMock()
    chat_history = MagicMock()
    faq_service = MagicMock()
    db_manager = MagicMock()
    http_client = MagicMock()

    # 1. Verify when parsed from LLM_FALLBACK_CHAIN string via Settings constructor
    settings_from_str = Settings(
        telegram_bot_token=mock_settings.telegram_bot_token,
        telegram_support_group_chat_id=mock_settings.telegram_support_group_chat_id,
        telegram_support_admin_username=mock_settings.telegram_support_admin_username,
        telegram_support_admin_telegram_ids=mock_settings.telegram_support_admin_telegram_ids,
        llm_provider="deepseek",
        deepseek_api_key="sk-test-deepseek-key",
        deepseek_model="deepseek-chat",
        openrouter_api_key="test-openrouter-key",
        zai_api_key="test-zai-key",
        embedding_provider="gemini",
        gemini_api_key="test-gemini-key",
        remnawave_mcp_url="http://localhost:3100",
        healthcheck_port=8080,
        reasoning_effort="none",
        llm_fallback_chain=fallback_chain,
    )

    with pytest.raises(ValueError) as exc_info:
        create_llm_client(
            settings_from_str, mcp_router, chat_history, faq_service, db_manager, http_client
        )

    err = str(exc_info.value)
    assert expected_err_substring in err
    assert "REASONING_EFFORT='none'" in err
    assert http_client.mock_calls == []

    # 2. Also verify when configured via model_copy with parsed fallback targets
    settings_from_tuple = mock_settings.model_copy(
        update={
            "openrouter_api_key": "test-openrouter-key",
            "zai_api_key": "test-zai-key",
            "reasoning_effort": "none",
            "llm_fallback_chain": _parse_fallback_chain(fallback_chain),
        }
    )

    with pytest.raises(ValueError) as exc_info_tuple:
        create_llm_client(
            settings_from_tuple, mcp_router, chat_history, faq_service, db_manager, http_client
        )

    err_tuple = str(exc_info_tuple.value)
    assert expected_err_substring in err_tuple
    assert "REASONING_EFFORT='none'" in err_tuple
    assert http_client.mock_calls == []


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
        patch("app.main.sync_legacy_schema", new_callable=AsyncMock) as mock_sync_schema,
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
        mock_mcp.list_tools = MagicMock(
            return_value=[McpTool(name="nodes_list", description="List nodes")]
        )
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
        mock_sync_schema.assert_awaited_once_with(mock_db.engine)
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


@pytest.mark.asyncio
async def test_main_configures_logging_from_settings(mock_settings: Settings) -> None:
    """Verify that main calls setup_logging with settings.bot_log_level."""
    from app.main import main

    with (
        patch("app.main.get_settings", return_value=mock_settings),
        patch("app.main.setup_logging") as mock_setup_logging,
        patch("app.main.get_db_manager") as mock_get_db,
        patch("app.main.HttpMcpClient") as mock_mcp,
        patch("app.main.FaqInitializer") as mock_faq_init,
        patch("app.main.KnowledgeGapService") as mock_gap_service,
        patch("app.main.Dispatcher") as mock_dp,
        patch("app.main.Bot") as mock_bot,
        patch("app.main.sync_legacy_schema", new_callable=AsyncMock),
        patch("app.main.start_health_server", new_callable=AsyncMock),
        patch("app.main.stop_health_server", new_callable=AsyncMock),
    ):
        mock_get_db.return_value.init_models = AsyncMock()
        mock_get_db.return_value.close = AsyncMock()
        mock_mcp.return_value.init = AsyncMock(return_value=False)
        mock_mcp.return_value.close = AsyncMock()
        mock_faq_init.return_value.run = AsyncMock()
        mock_gap_service.return_value.init_schema = AsyncMock()
        mock_bot.return_value.session.close = AsyncMock()
        mock_bot.return_value.set_my_commands = AsyncMock()
        mock_dp.return_value.start_polling = AsyncMock()

        await main()
        mock_setup_logging.assert_called_once_with(mock_settings.bot_log_level)


@pytest.mark.asyncio
async def test_main_stops_before_network_when_settings_fail_validation() -> None:
    """Startup validation failure must stop main before network setup or client creation."""
    from pydantic import ValidationError

    from app.main import main

    with (
        patch(
            "app.main.get_settings",
            side_effect=ValidationError.from_exception_data(
                "Settings",
                [
                    {
                        "type": "value_error",
                        "loc": ("bot_log_level",),
                        "input": None,
                        "ctx": {"error": "Invalid"},
                    }
                ],
                hide_input=True,
            ),
        ),
        patch("app.main.Bot") as mock_bot,
        patch("app.main.get_db_manager") as mock_db,
        patch("app.main.httpx.AsyncClient") as mock_http,
    ):
        with pytest.raises(ValidationError):
            await main()

        mock_bot.assert_not_called()
        mock_db.assert_not_called()
        mock_http.assert_not_called()
