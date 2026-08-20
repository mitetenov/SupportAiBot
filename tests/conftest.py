"""Global test fixtures and configuration."""

import pytest


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
