"""Configuration management and startup validation using Pydantic Settings."""

import logging
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

VALID_LLM_PROVIDERS: list[str] = ["deepseek", "gemini", "openai"]
VALID_EMBEDDING_PROVIDERS: list[str] = ["gemini", "openai"]


def reveal(value: SecretStr | str | None) -> str:
    """Return the plain text behind a secret, or "" when it is unset.

    Every read of a credential goes through here, so grepping for the name finds
    every place a secret leaves the settings object.
    """
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _require_text(value: SecretStr | str | None, message: str) -> None:
    """Ensure the given value contains non-whitespace text, otherwise raise ValueError."""
    if not reveal(value).strip():
        raise ValueError(message)


class Settings(BaseSettings):
    """Application settings loaded from environment variables and validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        enable_decoding=False,
    )

    # Database (PostgreSQL / PGVector)
    pgvector_host: str = "pgvector"
    pgvector_port: int = 5432
    pgvector_db: str = "vpnsupport"
    pgvector_user: str = "bot"
    pgvector_password: SecretStr = SecretStr("")

    # Chat history & conversation
    chat_history_max_messages: int = 20
    chat_history_ttl_days: int = 7
    conversation_operator_suppression_window_minutes: int = 30
    conversation_last_query_ttl_hours: int = 6

    # Providers
    llm_provider: str = "deepseek"
    embedding_provider: str = "gemini"

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_support_group_chat_id: int = 0
    telegram_support_admin_username: str = ""
    telegram_support_admin_telegram_ids: set[int] = Field(default_factory=set)
    telegram_buffer_window_ms: int = 2500
    telegram_buffer_max_messages: int = 5

    # DeepSeek
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str | None = None

    # Gemini
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str | None = None

    # OpenAI
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str | None = "gpt-5.6-luna"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_temperature: float | None = None

    # Remnawave MCP
    remnawave_mcp_url: str = "http://localhost:3100"
    remnawave_base_url: str = ""
    remnawave_api_token: SecretStr = SecretStr("")
    remnawave_mcp_readonly: bool = False

    # Server / Healthcheck
    healthcheck_port: int = 8080

    @property
    def database_url(self) -> str:
        """Construct the asyncpg connection string."""
        return (
            f"postgresql+asyncpg://{self.pgvector_user}:{reveal(self.pgvector_password)}"
            f"@{self.pgvector_host}:{self.pgvector_port}/{self.pgvector_db}"
        )

    @field_validator("telegram_support_admin_telegram_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Any) -> set[int]:
        """Parse comma-separated admin IDs string or collection into a set of integers."""
        if v is None:
            return set()
        if isinstance(v, set):
            return {int(x) for x in v if str(x).strip().lstrip("-+").isdigit()}
        if isinstance(v, (list, tuple)):
            result: set[int] = set()
            for x in v:
                try:
                    result.add(int(x))
                except (ValueError, TypeError):
                    continue
            return result
        if isinstance(v, str):
            v_str = v.strip()
            if not v_str:
                return set()
            result: set[int] = set()
            for item in v_str.split(","):
                item_str = item.strip()
                if not item_str:
                    continue
                try:
                    result.add(int(item_str))
                except ValueError:
                    logger.warning("Invalid admin Telegram ID ignored: %s", item_str)
                    continue
            return result
        return set()

    @model_validator(mode="after")
    def validate_startup(self) -> Settings:
        """Replicate the validation rules from StartupValidator.java."""
        # 1. Validate Telegram
        _require_text(
            self.telegram_bot_token,
            "TELEGRAM_BOT_TOKEN не задан. Получите токен у @BotFather и добавьте в .env: TELEGRAM_BOT_TOKEN=<токен>",
        )

        if self.telegram_support_group_chat_id == 0:
            raise ValueError(
                "TELEGRAM_SUPPORT_GROUP_CHAT_ID не задан. "
                "Создайте супергруппу в Telegram, включите Topics (форум) "
                "и укажите её ID в .env: TELEGRAM_SUPPORT_GROUP_CHAT_ID=-100XXXXXXXXXX"
            )
        if self.telegram_support_group_chat_id > 0:
            raise ValueError(
                f"TELEGRAM_SUPPORT_GROUP_CHAT_ID должен быть отрицательным числом (ID супергруппы). "
                f"Сейчас задан положительный ID: {self.telegram_support_group_chat_id}. "
                f"Убедитесь, что группа преобразована в супергруппу (включены Topics/форум). "
                f"ID супергруппы начинается с -100, например: -1001234567890"
            )

        if not self.telegram_support_admin_telegram_ids:
            logger.warning(
                "TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS не задан — "
                "уведомления об ошибках и запросы оператора не будут отправляться администраторам. "
                "Добавьте в .env: TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS=123456789,987654321"
            )

        # 2. Validate LLM Provider
        if not self.llm_provider or not self.llm_provider.strip():
            raise ValueError(
                "LLM_PROVIDER не задан. Укажите один из: "
                + ", ".join(VALID_LLM_PROVIDERS)
                + "\nПример: LLM_PROVIDER=openai"
            )

        normalized_llm = self.llm_provider.strip().lower()
        if normalized_llm not in VALID_LLM_PROVIDERS:
            raise ValueError(
                f"Неизвестный LLM_PROVIDER: '{self.llm_provider}'. "
                f"Допустимые значения: {', '.join(VALID_LLM_PROVIDERS)}"
                f"\nПроверьте опечатку в .env: LLM_PROVIDER={self.llm_provider}"
            )
        self.llm_provider = normalized_llm

        if normalized_llm == "deepseek":
            _require_text(
                self.deepseek_api_key,
                "DEEPSEEK_API_KEY не задан. Получите ключ на https://platform.deepseek.com/api_keys "
                "и добавьте в .env: DEEPSEEK_API_KEY=sk-...",
            )
            _require_text(
                self.deepseek_model,
                "DEEPSEEK_MODEL не задан. Укажите модель, например: DEEPSEEK_MODEL=deepseek-v4-flash",
            )
        elif normalized_llm == "gemini":
            _require_text(
                self.gemini_api_key,
                "GEMINI_API_KEY не задан. Получите ключ в Google AI Studio: "
                "https://aistudio.google.com/apikey и добавьте в .env: GEMINI_API_KEY=...",
            )
            _require_text(
                self.gemini_model,
                "GEMINI_MODEL не задан. Укажите модель, например: GEMINI_MODEL=gemini-3.5-flash-lite",
            )
        elif normalized_llm == "openai":
            _require_text(
                self.openai_api_key,
                "OPENAI_API_KEY не задан. Получите ключ на https://platform.openai.com/api-keys "
                "и добавьте в .env: OPENAI_API_KEY=sk-...",
            )
            key = reveal(self.openai_api_key).strip()
            if not key.startswith("sk-"):
                prefix = key[:5]
                raise ValueError(
                    f"OPENAI_API_KEY должен начинаться с 'sk-'. "
                    f"Проверьте, что вы не перепутали его с Telegram-токеном или ключом другого провайдера. "
                    f"Текущее значение начинается с: '{prefix}...'"
                )
            _require_text(
                self.openai_model,
                "OPENAI_MODEL не задан. Укажите модель, например: OPENAI_MODEL=gpt-5.6-luna",
            )

        # 3. Validate Embedding Provider
        if not self.embedding_provider or not self.embedding_provider.strip():
            raise ValueError(
                "EMBEDDING_PROVIDER не задан. "
                f"Допустимые значения: {', '.join(VALID_EMBEDDING_PROVIDERS)}"
                f"\nПроверьте .env: EMBEDDING_PROVIDER="
            )

        normalized_emb = self.embedding_provider.strip().lower()
        if normalized_emb not in VALID_EMBEDDING_PROVIDERS:
            raise ValueError(
                f"Неизвестный EMBEDDING_PROVIDER: '{self.embedding_provider}'. "
                f"Допустимые значения: {', '.join(VALID_EMBEDDING_PROVIDERS)}"
                f"\nПроверьте .env: EMBEDDING_PROVIDER={self.embedding_provider}"
            )
        self.embedding_provider = normalized_emb

        if normalized_emb == "openai":
            if not reveal(self.openai_api_key).strip():
                raise ValueError(
                    "EMBEDDING_PROVIDER=openai, но OPENAI_API_KEY не задан. "
                    "Добавьте в .env: OPENAI_API_KEY=sk-... "
                    "или смените провайдера: EMBEDDING_PROVIDER=gemini"
                )
            if not reveal(self.openai_api_key).strip().startswith("sk-"):
                raise ValueError(
                    "EMBEDDING_PROVIDER=openai, но OPENAI_API_KEY имеет неверный формат "
                    "(должен начинаться с 'sk-'). Проверьте .env: OPENAI_API_KEY"
                )
        elif normalized_emb == "gemini":
            if not reveal(self.gemini_api_key).strip():
                raise ValueError(
                    "EMBEDDING_PROVIDER=gemini, но GEMINI_API_KEY не задан. "
                    "Добавьте в .env: GEMINI_API_KEY=... "
                    "или смените провайдера: EMBEDDING_PROVIDER=openai"
                )

        # 4. Validate Remnawave
        _require_text(
            self.remnawave_mcp_url,
            "REMNAWAVE_MCP_URL не задан. Укажите URL MCP-сервера Remnawave, "
            "например: REMNAWAVE_MCP_URL=http://mcp-remnawave:3100",
        )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
