"""Global test fixtures and configuration."""

import os

import pytest

from app.config import Settings, get_settings

# Every field the bot reads from the environment. Left in place, a developer's
# own .env or exported shell variables leak into Settings(...) and quietly change
# what the validation tests are asserting about.
_SETTINGS_ENV_PREFIXES = (
    "TELEGRAM_",
    "LLM_",
    "EMBEDDING_",
    "DEEPSEEK_",
    "GEMINI_",
    "OPENAI_",
    "REMNAWAVE_",
    "PGVECTOR_",
    "CHAT_HISTORY_",
    "CONVERSATION_",
    "HEALTHCHECK_",
)


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build Settings from explicit arguments only — no .env, no ambient env vars."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in list(os.environ):
        if name.startswith(_SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def valid_settings_dict() -> dict[str, object]:
    """Return a dictionary of settings that passes all startup validation."""
    return {
        "telegram_bot_token": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        "telegram_support_group_chat_id": -1001234567890,
        "telegram_support_admin_username": "admin",
        "telegram_support_admin_telegram_ids": "123456789,987654321",
        "llm_provider": "deepseek",
        "deepseek_api_key": "sk-deepseek-test-key",
        "deepseek_model": "deepseek-chat",
        "embedding_provider": "gemini",
        "gemini_api_key": "gemini-test-key",
        "gemini_model": "gemini-2.5-flash",
        "remnawave_mcp_url": "http://localhost:3100",
        "pgvector_host": "localhost",
        "pgvector_port": 5432,
        "pgvector_db": "vpnsupport",
        "pgvector_user": "bot",
        "pgvector_password": "secret_password",
    }


@pytest.fixture(autouse=True)
def _skip_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the wall-clock cost out of the retry policy.

    Several tests drive a provider that answers 500 or 429; with the real
    backoff each of them would sit in asyncio.sleep for over a second.
    """

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.retry._sleep", instant)
