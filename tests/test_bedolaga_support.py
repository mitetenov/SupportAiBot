"""Unit tests for assembling the Bedolaga ticket integration."""

from unittest.mock import MagicMock

import httpx
from aiohttp import web

from app.bedolaga import create_ticket_support
from app.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "telegram_support_group_chat_id": -1001234567890,
        "llm_provider": "deepseek",
        "deepseek_api_key": "sk-test",
        "deepseek_model": "deepseek-chat",
        "embedding_provider": "gemini",
        "gemini_api_key": "test",
        "gemini_model": "gemini-2.5-flash",
        "pgvector_password": "secret",
    }
    base.update(overrides)
    return Settings(**base)


def _create(settings: Settings):
    return create_ticket_support(
        settings=settings,
        http_client=MagicMock(spec=httpx.AsyncClient),
        llm_client=MagicMock(),
        db_manager=MagicMock(),
        forwarder=MagicMock(),
        admin_notifier=MagicMock(),
        rate_limiter=MagicMock(),
        knowledge_gap_service=MagicMock(),
        conversation_state=MagicMock(),
    )


class TestCreateTicketSupport:
    """Nothing is built, mounted or scheduled while the integration is off."""

    def test_returns_none_when_disabled(self) -> None:
        assert _create(_settings()) is None

    def test_builds_the_integration_when_enabled(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
            )
        )
        assert support is not None
        assert support.poller.client is support.answerer.client

    def test_mounts_the_webhook_route(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
                bedolaga_webhook_path="/hooks/tickets",
            )
        )
        assert support is not None
        app = web.Application()
        support.register_routes(app)
        routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
        assert ("POST", "/hooks/tickets") in routes

    def test_builds_a_maintenance_job_on_the_configured_interval(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
                bedolaga_poll_interval_seconds=30,
            )
        )
        assert support is not None
        job = support.maintenance_job()
        assert job.interval_seconds == 30
        assert "bedolaga" in job.name
