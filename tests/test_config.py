"""Tests for configuration settings and startup validation rules."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings, reveal


class TestStartupValidator:
    """Test suite matching all validation rules from StartupValidator.java."""

    def test_should_validate_deepseek_provider(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify valid DeepSeek settings pass validation."""
        settings = Settings(**valid_settings_dict)
        assert settings.llm_provider == "deepseek"
        assert reveal(settings.deepseek_api_key) == "sk-deepseek-test-key"

    def test_should_normalize_reasoning_effort(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict["reasoning_effort"] = " MAX "
        settings = Settings(**valid_settings_dict)
        assert settings.reasoning_effort == "max"

    def test_should_reject_unknown_reasoning_effort(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict["reasoning_effort"] = "auto"
        with pytest.raises(ValidationError) as exc_info:
            Settings(**valid_settings_dict)
        assert "REASONING_EFFORT" in str(exc_info.value)

    def test_should_validate_gemini_provider(self, valid_settings_dict: dict[str, object]) -> None:
        """Verify valid Gemini settings pass validation."""
        valid_settings_dict["llm_provider"] = "gemini"
        valid_settings_dict["gemini_api_key"] = "gemini-test-key"
        valid_settings_dict["gemini_model"] = "gemini-2.5-flash"
        valid_settings_dict["embedding_provider"] = "gemini"

        settings = Settings(**valid_settings_dict)
        assert settings.llm_provider == "gemini"
        assert reveal(settings.gemini_api_key) == "gemini-test-key"

    def test_should_validate_openai_provider(self, valid_settings_dict: dict[str, object]) -> None:
        """Verify valid OpenAI settings pass validation."""
        valid_settings_dict["llm_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "sk-proj-test123456"
        valid_settings_dict["openai_model"] = "gpt-5.6-luna"
        valid_settings_dict["embedding_provider"] = "openai"

        settings = Settings(**valid_settings_dict)
        assert settings.llm_provider == "openai"
        assert reveal(settings.openai_api_key) == "sk-proj-test123456"

    def test_should_throw_when_bot_token_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when TELEGRAM_BOT_TOKEN is missing or None."""
        valid_settings_dict["telegram_bot_token"] = ""
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)

    def test_should_throw_when_bot_token_blank(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when TELEGRAM_BOT_TOKEN is whitespace only."""
        valid_settings_dict["telegram_bot_token"] = "   "
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)

    def test_should_throw_when_support_group_chat_id_is_zero(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when TELEGRAM_SUPPORT_GROUP_CHAT_ID is zero."""
        valid_settings_dict["telegram_support_group_chat_id"] = 0
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "TELEGRAM_SUPPORT_GROUP_CHAT_ID" in str(exc_info.value)

    def test_should_throw_when_support_group_chat_id_is_positive(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when TELEGRAM_SUPPORT_GROUP_CHAT_ID is positive."""
        valid_settings_dict["telegram_support_group_chat_id"] = 123456789
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "отрицательным" in str(exc_info.value)

    def test_should_throw_for_unknown_provider(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error for unknown LLM provider."""
        valid_settings_dict["llm_provider"] = "unknown"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "Неизвестный LLM_PROVIDER" in str(exc_info.value)
        assert "deepseek" in str(exc_info.value)

    def test_should_throw_for_typo_in_provider(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error for typo in LLM provider."""
        valid_settings_dict["llm_provider"] = "openei"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "Неизвестный LLM_PROVIDER" in str(exc_info.value)
        assert "openai" in str(exc_info.value)

    def test_should_throw_when_deepseek_api_key_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when DeepSeek API key is missing."""
        valid_settings_dict["llm_provider"] = "deepseek"
        valid_settings_dict["deepseek_api_key"] = None
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "DEEPSEEK_API_KEY" in str(exc_info.value)

    def test_should_throw_when_deepseek_model_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when DeepSeek model is missing."""
        valid_settings_dict["llm_provider"] = "deepseek"
        valid_settings_dict["deepseek_model"] = ""
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "DEEPSEEK_MODEL" in str(exc_info.value)

    def test_should_throw_when_openai_api_key_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when OpenAI API key is missing."""
        valid_settings_dict["llm_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = None
        valid_settings_dict["openai_model"] = "gpt-4"
        valid_settings_dict["embedding_provider"] = "gemini"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "OPENAI_API_KEY" in str(exc_info.value)

    def test_should_throw_when_openai_api_key_wrong_format(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when OpenAI API key does not start with 'sk-'."""
        valid_settings_dict["llm_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "not-sk-prefix"
        valid_settings_dict["openai_model"] = "gpt-4"
        valid_settings_dict["embedding_provider"] = "gemini"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "начинаться с 'sk-'" in str(exc_info.value)

    def test_should_throw_when_openai_model_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when OpenAI model is missing."""
        valid_settings_dict["llm_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "sk-valid-key"
        valid_settings_dict["openai_model"] = ""
        valid_settings_dict["embedding_provider"] = "gemini"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "OPENAI_MODEL" in str(exc_info.value)

    def test_should_throw_when_gemini_api_key_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when Gemini API key is missing."""
        valid_settings_dict["llm_provider"] = "gemini"
        valid_settings_dict["gemini_api_key"] = None
        valid_settings_dict["gemini_model"] = "gemini-pro"
        valid_settings_dict["embedding_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "sk-valid"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "GEMINI_API_KEY" in str(exc_info.value)

    def test_should_throw_when_gemini_model_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when Gemini model is missing."""
        valid_settings_dict["llm_provider"] = "gemini"
        valid_settings_dict["gemini_api_key"] = "gemini-key"
        valid_settings_dict["gemini_model"] = ""
        valid_settings_dict["embedding_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "sk-valid"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "GEMINI_MODEL" in str(exc_info.value)

    def test_should_throw_when_remnawave_mcp_url_missing(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when REMNAWAVE_MCP_URL is missing."""
        valid_settings_dict["remnawave_mcp_url"] = ""
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "REMNAWAVE_MCP_URL" in str(exc_info.value)

    def test_should_throw_for_unknown_embedding_provider(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error for unknown embedding provider."""
        valid_settings_dict["embedding_provider"] = "badprovider"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "Неизвестный EMBEDDING_PROVIDER" in str(exc_info.value)

    def test_should_throw_when_embedding_openai_but_no_api_key(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when embedding provider is openai but OPENAI_API_KEY is missing."""
        valid_settings_dict["embedding_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = None
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "OPENAI_API_KEY" in str(exc_info.value)

    def test_should_throw_when_embedding_openai_but_wrong_key_format(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when embedding provider is openai but key does not start with sk-."""
        valid_settings_dict["embedding_provider"] = "openai"
        valid_settings_dict["openai_api_key"] = "wrong-key"
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "sk-" in str(exc_info.value)

    def test_should_throw_when_embedding_gemini_but_no_api_key(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Verify error when embedding provider is gemini but GEMINI_API_KEY is missing."""
        valid_settings_dict["embedding_provider"] = "gemini"
        valid_settings_dict["gemini_api_key"] = None
        with pytest.raises((ValueError, ValidationError)) as exc_info:
            Settings(**valid_settings_dict)
        assert "GEMINI_API_KEY" in str(exc_info.value)


class TestTelegramAdminIdsParsing:
    """Test suite matching all parsing scenarios from TelegramPropertiesTest.java."""

    @pytest.mark.parametrize("blank_input", ["", "  ", "\t", "\n", None])
    def test_should_return_empty_set_for_blank_admin_ids(
        self, valid_settings_dict: dict[str, object], blank_input: str | None
    ) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = blank_input
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == set()

    def test_should_parse_single_admin_id(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = "12345"
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == {12345}

    def test_should_parse_multiple_admin_ids(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = "12345,67890,11111"
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == {12345, 67890, 11111}

    def test_should_handle_whitespace_in_admin_ids(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = " 12345 , 67890 , 11111 "
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == {12345, 67890, 11111}

    def test_should_filter_empty_segments(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = "12345,,67890,"
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == {12345, 67890}

    def test_should_skip_non_numeric_admin_id(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = "abc"
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == set()

    def test_should_skip_invalid_and_keep_valid_admin_ids(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        valid_settings_dict["telegram_support_admin_telegram_ids"] = "12345,abc,67890"
        settings = Settings(**valid_settings_dict)
        assert settings.telegram_support_admin_telegram_ids == {12345, 67890}


class TestSettingsHelperProperties:
    """Test helper properties on Settings like database_url."""

    def test_database_url_formatting(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["pgvector_host"] = "db.internal"
        valid_settings_dict["pgvector_port"] = 5433
        valid_settings_dict["pgvector_db"] = "custom_db"
        valid_settings_dict["pgvector_user"] = "custom_user"
        valid_settings_dict["pgvector_password"] = "p@ss:word"

        settings = Settings(**valid_settings_dict)
        assert (
            settings.database_url
            == "postgresql+asyncpg://custom_user:p@ss:word@db.internal:5433/custom_db"
        )

    def test_get_settings_caching(
        self, valid_settings_dict: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for k, v in valid_settings_dict.items():
            if isinstance(v, set):
                monkeypatch.setenv(k.upper(), ",".join(map(str, v)))
            elif v is not None:
                monkeypatch.setenv(k.upper(), str(v))
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        get_settings.cache_clear()


class TestSecretsAreNotPrintable:
    """A stray log of the settings object must not print credentials."""

    def test_repr_hides_every_secret(self, valid_settings_dict: dict[str, object]) -> None:
        valid_settings_dict["openai_api_key"] = "sk-proj-test123456"
        settings = Settings(**valid_settings_dict)

        printed = repr(settings) + str(settings) + str(settings.model_dump())

        for secret in (
            "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "sk-deepseek-test-key",
            "gemini-test-key",
            "sk-proj-test123456",
            "secret_password",
        ):
            assert secret not in printed

    def test_reveal_returns_the_plain_value(self, valid_settings_dict: dict[str, object]) -> None:
        settings = Settings(**valid_settings_dict)

        assert reveal(settings.telegram_bot_token) == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        assert reveal(settings.deepseek_api_key) == "sk-deepseek-test-key"

    def test_reveal_tolerates_unset_and_plain_values(self) -> None:
        assert reveal(None) == ""
        assert reveal("plain") == "plain"

    def test_database_url_still_carries_the_password(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        settings = Settings(**valid_settings_dict)
        assert "secret_password" in settings.database_url


class TestBedolagaSettings:
    """The Bedolaga ticket integration is off until it is fully configured."""

    def test_disabled_by_default(self, valid_settings_dict: dict[str, object]) -> None:
        settings = Settings(**valid_settings_dict)
        assert settings.bedolaga_enabled is False
        assert settings.bedolaga_webhook_path == "/bedolaga/webhook"
        assert settings.bedolaga_poll_interval_seconds == 60
        assert settings.bedolaga_max_concurrent_tickets == 5

    def test_enabled_requires_api_url(self, valid_settings_dict: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_API_URL"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_key="key",
                bedolaga_webhook_secret="shhh",
            )

    def test_enabled_requires_api_key(self, valid_settings_dict: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_API_KEY"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_webhook_secret="shhh",
            )

    def test_enabled_requires_webhook_secret(self, valid_settings_dict: dict[str, object]) -> None:
        """An unsigned webhook schedules model calls for anyone who reaches the port."""
        with pytest.raises(ValidationError, match="BEDOLAGA_WEBHOOK_SECRET"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="key",
            )

    def test_enabled_with_full_configuration(self, valid_settings_dict: dict[str, object]) -> None:
        settings = Settings(
            **valid_settings_dict,
            bedolaga_enabled=True,
            bedolaga_api_url="http://bedolaga:8080/",
            bedolaga_api_key="secret-token",
            bedolaga_webhook_secret="shhh",
        )
        assert settings.bedolaga_enabled is True
        assert reveal(settings.bedolaga_api_key) == "secret-token"
        assert reveal(settings.bedolaga_webhook_secret) == "shhh"

    def test_enabled_rejects_a_concurrency_cap_below_one(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        """Zero would park every ticket on a semaphore that never opens."""
        with pytest.raises(ValidationError, match="BEDOLAGA_MAX_CONCURRENT_TICKETS"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="key",
                bedolaga_webhook_secret="shhh",
                bedolaga_max_concurrent_tickets=0,
            )

    def test_enabled_accepts_a_custom_concurrency_cap(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        settings = Settings(
            **valid_settings_dict,
            bedolaga_enabled=True,
            bedolaga_api_url="http://bedolaga:8080",
            bedolaga_api_key="key",
            bedolaga_webhook_secret="shhh",
            bedolaga_max_concurrent_tickets=12,
        )
        assert settings.bedolaga_max_concurrent_tickets == 12

    def test_enabled_rejects_poll_interval_below_one(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_POLL_INTERVAL_SECONDS"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="key",
                bedolaga_webhook_secret="shhh",
                bedolaga_poll_interval_seconds=0,
            )

    def test_enabled_rejects_webhook_path_without_leading_slash(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_WEBHOOK_PATH"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="key",
                bedolaga_webhook_secret="shhh",
                bedolaga_webhook_path="bedolaga/webhook",
            )


class TestBotLogLevelSettings:
    """Tests for BOT_LOG_LEVEL configuration and validation."""

    def test_should_default_bot_log_level_to_info(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        settings = Settings(**valid_settings_dict)
        assert settings.bot_log_level == "INFO"
        assert settings.log_level == "INFO"

    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            ("TRACE", "TRACE"),
            ("INFO", "INFO"),
            ("ERROR", "ERROR"),
            ("trace", "TRACE"),
            ("info", "INFO"),
            ("error", "ERROR"),
            ("  Trace  ", "TRACE"),
            ("\tinfo\n", "INFO"),
            ("  ERROR  ", "ERROR"),
        ],
    )
    def test_should_accept_valid_log_levels_case_insensitively_with_whitespace(
        self,
        valid_settings_dict: dict[str, object],
        input_value: str,
        expected: str,
    ) -> None:
        valid_settings_dict["bot_log_level"] = input_value
        settings = Settings(**valid_settings_dict)
        assert settings.bot_log_level == expected
        assert settings.log_level == expected

    @pytest.mark.parametrize(
        "invalid_value",
        [
            "",
            "   ",
            "\t\n",
            "DEBUG",
            "WARNING",
            "CRITICAL",
            "VERBOSE",
            "NONE",
            "ALL",
            "UNKNOWN",
        ],
    )
    def test_should_reject_invalid_log_levels(
        self,
        valid_settings_dict: dict[str, object],
        invalid_value: str,
    ) -> None:
        valid_settings_dict["bot_log_level"] = invalid_value
        with pytest.raises(ValidationError) as exc_info:
            Settings(**valid_settings_dict)
        error_text = str(exc_info.value)
        assert "BOT_LOG_LEVEL" in error_text
        assert "TRACE" in error_text
        assert "INFO" in error_text
        assert "ERROR" in error_text

    def test_validation_error_must_not_reveal_raw_input_value_or_credentials(
        self, valid_settings_dict: dict[str, object]
    ) -> None:
        secret_value = "sk-secret-credentials-token-xyz987"
        valid_settings_dict["bot_log_level"] = secret_value
        with pytest.raises(ValidationError) as exc_info:
            Settings(**valid_settings_dict)

        error_message = str(exc_info.value)
        assert secret_value not in error_message
        for err in exc_info.value.errors():
            assert secret_value not in str(err)
            assert err.get("input") is None

    def test_bot_log_level_read_from_environment(
        self,
        valid_settings_dict: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BOT_LOG_LEVEL", "  trace  ")
        settings = Settings(**valid_settings_dict)
        assert settings.bot_log_level == "TRACE"
