"""Configuration management and startup validation using Pydantic Settings."""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

VALID_LOG_LEVELS: tuple[str, ...] = ("TRACE", "INFO", "ERROR")
VALID_LLM_PROVIDERS: list[str] = ["deepseek", "gemini", "openai", "groq"]
VALID_EMBEDDING_PROVIDERS: list[str] = ["gemini", "openai"]
VALID_REASONING_EFFORTS: list[str] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class LlmProviderTarget:
    """One explicitly ordered backup model configured for a chat turn."""

    provider: str
    model: str


def _parse_fallback_chain(value: Any) -> tuple[LlmProviderTarget, ...]:
    """Parse the comma-separated ``provider:model`` fallback configuration."""
    if value is None or value == "":
        return ()
    if isinstance(value, tuple) and all(isinstance(target, LlmProviderTarget) for target in value):
        return value
    if not isinstance(value, str):
        raise ValueError("LLM_FALLBACK_CHAIN должен быть строкой provider:model через запятую")

    targets: list[LlmProviderTarget] = []
    for raw_target in value.split(","):
        target = raw_target.strip()
        if not target:
            raise ValueError("LLM_FALLBACK_CHAIN не должен содержать пустые элементы")
        provider, separator, model = target.partition(":")
        provider = provider.strip().lower()
        model = model.strip()
        if not separator or not provider or not model:
            raise ValueError("Каждый элемент LLM_FALLBACK_CHAIN должен иметь формат provider:model")
        if provider not in VALID_LLM_PROVIDERS:
            raise ValueError(
                f"Неизвестный провайдер в LLM_FALLBACK_CHAIN: '{provider}'. "
                f"Допустимые значения: {', '.join(VALID_LLM_PROVIDERS)}"
            )
        targets.append(LlmProviderTarget(provider=provider, model=model))
    return tuple(targets)


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

    # Logging
    bot_log_level: str = "INFO"

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except ValidationError as exc:
            line_errors: list[InitErrorDetails] = []
            for err in exc.errors(include_url=False, include_context=True, include_input=False):
                msg = err["msg"]
                if msg.startswith("Value error, "):
                    msg = msg[len("Value error, ") :]
                line_errors.append(
                    InitErrorDetails(
                        type="value_error",
                        loc=err["loc"],
                        input=None,
                        ctx={"error": msg},
                    )
                )
            raise ValidationError.from_exception_data(
                exc.title, line_errors, hide_input=True
            ) from None

    @property
    def log_level(self) -> str:
        """Return the normalized logging level."""
        return self.bot_log_level

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
    llm_provider: str = "openai"
    llm_fallback_chain: tuple[LlmProviderTarget, ...] = ()
    embedding_provider: str = "gemini"
    reasoning_effort: str = "none"

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

    # Groq
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str | None = "llama-3.3-70b-versatile"

    # Remnawave MCP
    remnawave_mcp_url: str = "http://localhost:3100"
    remnawave_base_url: str = ""
    remnawave_api_token: SecretStr = SecretStr("")
    remnawave_mcp_readonly: bool = False

    # Bedolaga MCP — the personal MCP tools the bot exposes to Telegram support
    # (separate from BEDOLAGA_ENABLED below, which governs the webhook/poller
    # ticket handling and the direct Bedolaga Web API client).
    bedolaga_mcp_enabled: bool = False
    bedolaga_mcp_url: str = ""

    # Bedolaga tickets
    bedolaga_enabled: bool = False
    bedolaga_api_url: str = ""
    bedolaga_api_key: SecretStr = SecretStr("")
    bedolaga_webhook_secret: SecretStr = SecretStr("")
    bedolaga_webhook_path: str = "/bedolaga/webhook"
    bedolaga_poll_interval_seconds: int = 60
    bedolaga_max_concurrent_tickets: int = 5

    # Server / Healthcheck
    healthcheck_port: int = 8080

    @property
    def database_url(self) -> str:
        """Construct the asyncpg connection string."""
        return (
            f"postgresql+asyncpg://{self.pgvector_user}:{reveal(self.pgvector_password)}"
            f"@{self.pgvector_host}:{self.pgvector_port}/{self.pgvector_db}"
        )

    @field_validator("bot_log_level", mode="before")
    @classmethod
    def normalize_bot_log_level(cls, value: Any) -> str:
        """Validate and normalize BOT_LOG_LEVEL strictly to TRACE, INFO, or ERROR."""
        if value is None:
            return "INFO"
        if not isinstance(value, str):
            raise ValueError(
                "Недопустимое значение BOT_LOG_LEVEL. Допустимые значения: TRACE, INFO, ERROR"
            )
        normalized = value.strip().upper()
        if not normalized or normalized not in VALID_LOG_LEVELS:
            raise ValueError(
                "Недопустимое значение BOT_LOG_LEVEL. Допустимые значения: TRACE, INFO, ERROR"
            )
        return normalized

    @field_validator("telegram_support_admin_telegram_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Any) -> set[int]:
        """Parse comma-separated admin IDs string or collection into a set of integers."""
        if v is None:
            return set()
        if isinstance(v, set):
            return {int(x) for x in v if str(x).strip().lstrip("-+").isdigit()}
        if isinstance(v, (list, tuple)):
            from_items: set[int] = set()
            for x in v:
                try:
                    from_items.add(int(x))
                except ValueError, TypeError:
                    continue
            return from_items
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
                    logger.error("Invalid admin Telegram ID ignored")
                    continue
            return result
        return set()

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, value: Any) -> str:
        """Normalize the provider-neutral reasoning level from REASONING_EFFORT."""
        normalized = str(value or "").strip().lower()
        if normalized not in VALID_REASONING_EFFORTS:
            raise ValueError(
                "Неизвестный REASONING_EFFORT: "
                f"'{value}'. Допустимые значения: {', '.join(VALID_REASONING_EFFORTS)}"
            )
        return normalized

    @field_validator("llm_fallback_chain", mode="before")
    @classmethod
    def parse_llm_fallback_chain(cls, value: Any) -> tuple[LlmProviderTarget, ...]:
        """Normalize LLM_FALLBACK_CHAIN while preserving its configured order."""
        return _parse_fallback_chain(value)

    def _configured_model(self, provider: str) -> str | None:
        return getattr(self, f"{provider}_model", None)

    def _validate_llm_target(self, provider: str, model: str | None = None) -> None:
        """Validate credentials and a model for one primary or fallback target."""
        configured_model = model or self._configured_model(provider)
        if provider == "deepseek":
            _require_text(
                self.deepseek_api_key,
                "DEEPSEEK_API_KEY не задан. Получите ключ на https://platform.deepseek.com/api_keys "
                "и добавьте в .env: DEEPSEEK_API_KEY=sk-...",
            )
            _require_text(
                configured_model,
                "DEEPSEEK_MODEL не задан. Укажите модель, например: DEEPSEEK_MODEL=deepseek-v4-flash",
            )
        elif provider == "gemini":
            _require_text(
                self.gemini_api_key,
                "GEMINI_API_KEY не задан. Получите ключ в Google AI Studio: "
                "https://aistudio.google.com/apikey и добавьте в .env: GEMINI_API_KEY=...",
            )
            _require_text(
                configured_model,
                "GEMINI_MODEL не задан. Укажите модель, например: GEMINI_MODEL=gemini-3.5-flash-lite",
            )
        elif provider == "openai":
            _require_text(
                self.openai_api_key,
                "OPENAI_API_KEY не задан. Получите ключ на https://platform.openai.com/api-keys "
                "и добавьте в .env: OPENAI_API_KEY=sk-...",
            )
            if not reveal(self.openai_api_key).strip().startswith("sk-"):
                raise ValueError(
                    "OPENAI_API_KEY должен начинаться с 'sk-'. Проверьте, что в .env указан ключ OpenAI."
                )
            _require_text(
                configured_model,
                "OPENAI_MODEL не задан. Укажите модель, например: OPENAI_MODEL=gpt-5.6-luna",
            )
        elif provider == "groq":
            _require_text(
                self.groq_api_key,
                "GROQ_API_KEY не задан. Получите ключ на https://console.groq.com/keys "
                "и добавьте в .env: GROQ_API_KEY=gsk_...",
            )
            _require_text(
                configured_model,
                "GROQ_MODEL не задан. Укажите модель, например: GROQ_MODEL=llama-3.3-70b-versatile",
            )

    @property
    def llm_provider_targets(self) -> tuple[LlmProviderTarget, ...]:
        """Return the primary target followed by configured fallbacks in order."""
        primary_model = self._configured_model(self.llm_provider)
        return (
            LlmProviderTarget(provider=self.llm_provider, model=primary_model or ""),
            *self.llm_fallback_chain,
        )

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
            logger.info(
                "TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS is empty; admin notifications are disabled"
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

        self._validate_llm_target(normalized_llm)
        for fallback_target in self.llm_fallback_chain:
            self._validate_llm_target(fallback_target.provider, fallback_target.model)

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

        # 5. Validate Bedolaga MCP (independent client, separate from the tickets)
        if self.bedolaga_mcp_enabled:
            _require_text(
                self.bedolaga_mcp_url,
                "BEDOLAGA_MCP_ENABLED=true, но BEDOLAGA_MCP_URL не задан. "
                "Укажите URL MCP-сервера Bedolaga, "
                "например: BEDOLAGA_MCP_URL=http://bedolaga-mcp:3100 "
                "или выключите MCP-интеграцию: BEDOLAGA_MCP_ENABLED=false",
            )

        # 6. Validate Bedolaga tickets
        if self.bedolaga_enabled:
            _require_text(
                self.bedolaga_api_url,
                "BEDOLAGA_ENABLED=true, но BEDOLAGA_API_URL не задан. "
                "Укажите URL Web API Bedolaga, "
                "например: BEDOLAGA_API_URL=http://bedolaga:8080 "
                "или выключите интеграцию: BEDOLAGA_ENABLED=false",
            )
            _require_text(
                self.bedolaga_api_key,
                "BEDOLAGA_ENABLED=true, но BEDOLAGA_API_KEY не задан. "
                "Создайте токен Web API в админке Bedolaga и добавьте в .env: "
                "BEDOLAGA_API_KEY=...",
            )
            # Without a secret the webhook endpoint accepts every delivery, and
            # it schedules model calls for anyone who can reach the bot's port —
            # bedolaga-net is a shared network, so "internal only" is no promise.
            _require_text(
                self.bedolaga_webhook_secret,
                "BEDOLAGA_ENABLED=true, но BEDOLAGA_WEBHOOK_SECRET не задан. "
                "Без него вебхук принимает любые запросы без проверки подписи. "
                "Придумайте случайную строку, укажите её при регистрации вебхуков "
                "в Bedolaga и добавьте в .env: BEDOLAGA_WEBHOOK_SECRET=...",
            )
            if not self.bedolaga_webhook_path.startswith("/"):
                raise ValueError(
                    f"BEDOLAGA_WEBHOOK_PATH должен начинаться со слэша '/'. "
                    f"Сейчас задано: '{self.bedolaga_webhook_path}'"
                )
            if self.bedolaga_poll_interval_seconds < 1:
                raise ValueError(
                    f"BEDOLAGA_POLL_INTERVAL_SECONDS должен быть не меньше 1. "
                    f"Сейчас задано: {self.bedolaga_poll_interval_seconds}. "
                    f"Значение по умолчанию: BEDOLAGA_POLL_INTERVAL_SECONDS=60"
                )
            # Zero would let the semaphore block every ticket forever; a
            # negative value is a typo. Nothing here is a security boundary —
            # the cap only protects the shared connection pool and the LLM
            # provider's rate limit from a large backlog.
            if self.bedolaga_max_concurrent_tickets < 1:
                raise ValueError(
                    f"BEDOLAGA_MAX_CONCURRENT_TICKETS должен быть не меньше 1. "
                    f"Сейчас задано: {self.bedolaga_max_concurrent_tickets}. "
                    f"Это ограничение числа тикетов, обрабатываемых одновременно; "
                    f"значение по умолчанию: BEDOLAGA_MAX_CONCURRENT_TICKETS=5"
                )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
