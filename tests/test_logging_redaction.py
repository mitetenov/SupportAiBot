"""Tests for logging credential redaction, immutability, and safe error metadata."""

import copy

from app.logging_redaction import (
    get_safe_error_metadata,
    redact_credentials_in_text,
    redact_data,
    register_secret,
    safe_serialize,
)


class TestCredentialRedactionInText:
    """Tests for redacting secrets in plain strings and URLs."""

    def test_should_redact_url_userinfo_credentials(self) -> None:
        url = "postgresql+asyncpg://bot:super_secret_db_pass@db.internal:5432/vpnsupport"
        redacted = redact_credentials_in_text(url)
        assert "super_secret_db_pass" not in redacted
        assert "bot" in redacted
        assert "db.internal:5432/vpnsupport" in redacted

    def test_should_redact_url_sensitive_query_parameters(self) -> None:
        url = (
            "https://api.example.com/webhook?token=my_secret_token_123"
            "&api_key=sk-abc12345678901234567890&user_id=42&page=2"
        )
        redacted = redact_credentials_in_text(url)
        assert "my_secret_token_123" not in redacted
        assert "sk-abc12345678901234567890" not in redacted
        assert "user_id=42" in redacted
        assert "page=2" in redacted

    def test_should_redact_telegram_bot_token(self) -> None:
        text = "Bot started with token 123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345678"
        redacted = redact_credentials_in_text(text)
        assert "123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345678" not in redacted
        assert "[REDACTED" in redacted

    def test_should_redact_openai_and_groq_api_keys(self) -> None:
        text = (
            "OpenAI: sk-proj-1234567890abcdefghijklmnopqrstuvwxyz "
            "Groq: gsk_1234567890abcdefghijklmnopqrstuvwxyz"
        )
        redacted = redact_credentials_in_text(text)
        assert "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz" not in redacted
        assert "gsk_1234567890abcdefghijklmnopqrstuvwxyz" not in redacted

    def test_should_redact_authorization_and_cookie_headers(self) -> None:
        headers_str = (
            "Authorization: Bearer secret-jwt-token-value-here\n"
            "Cookie: session_id=xyz789; auth_token=secret_abc\n"
            "Content-Type: application/json"
        )
        redacted = redact_credentials_in_text(headers_str)
        assert "secret-jwt-token-value-here" not in redacted
        assert "xyz789" not in redacted
        assert "secret_abc" not in redacted
        assert "Content-Type: application/json" in redacted

    def test_should_redact_registered_custom_secrets(self) -> None:
        register_secret("my_super_custom_secret_phrase")
        text = "Log message mentioning my_super_custom_secret_phrase in the middle"
        redacted = redact_credentials_in_text(text)
        assert "my_super_custom_secret_phrase" not in redacted
        assert "[REDACTED]" in redacted

    def test_should_not_redact_normal_personal_data_or_text(self) -> None:
        text = (
            "User ID: 987654321, username: @ivan_vpn, "
            "message: 'Здравствуйте, у меня не подключается WireGuard на сервере 5!'"
        )
        redacted = redact_credentials_in_text(text)
        assert "987654321" in redacted
        assert "@ivan_vpn" in redacted
        assert "WireGuard" in redacted
        assert "сервере 5" in redacted


class TestStructuredDataRedaction:
    """Tests for redacting dictionaries, lists, and nested objects."""

    def test_should_redact_sensitive_keys_recursively(self) -> None:
        data = {
            "token": "secret_token_val",
            "bot_token": "secret_bot_token",
            "user": {
                "id": 12345,
                "username": "alice",
                "password": "alice_plain_password",
                "api_key": "sk-secret-key-xyz",
            },
            "headers": {
                "Authorization": "Bearer top_secret",
                "Cookie": "session=abcdef",
                "Accept": "application/json",
            },
            "items": [
                {"name": "vpn1", "secret": "s3cr3t"},
                {"name": "vpn2", "safe_val": "public"},
            ],
        }

        redacted = redact_data(data)

        # Sensitive keys redacted
        assert redacted["token"] == "[REDACTED]"
        assert redacted["bot_token"] == "[REDACTED]"
        assert redacted["user"]["password"] == "[REDACTED]"
        assert redacted["user"]["api_key"] == "[REDACTED]"
        assert redacted["headers"]["Authorization"] == "[REDACTED]"
        assert redacted["headers"]["Cookie"] == "[REDACTED]"
        assert redacted["items"][0]["secret"] == "[REDACTED]"

        # Non-sensitive keys preserved
        assert redacted["user"]["id"] == 12345
        assert redacted["user"]["username"] == "alice"
        assert redacted["headers"]["Accept"] == "application/json"
        assert redacted["items"][1]["safe_val"] == "public"

    def test_immutability_original_object_must_not_be_mutated(self) -> None:
        original = {
            "token": "super_secret",
            "nested": {
                "api_key": "sk-secret",
                "data": [1, 2, {"password": "pass"}],
            },
        }
        original_copy = copy.deepcopy(original)

        redacted = redact_data(original)

        assert original == original_copy
        assert original["token"] == "super_secret"
        assert original["nested"]["api_key"] == "sk-secret"
        assert original["nested"]["data"][2]["password"] == "pass"  # type: ignore[index]
        assert redacted["token"] == "[REDACTED]"
        assert redacted is not original


class TestSafeSerializationAndErrorMetadata:
    """Tests for safe serialization and safe error extraction."""

    def test_safe_serialize_handles_unserializable_objects_safely(self) -> None:
        class Unserializable:
            def __str__(self) -> str:
                raise RuntimeError("cannot convert to str")

            def __repr__(self) -> str:
                raise RuntimeError("cannot convert to repr")

        obj = {"valid": 123, "bad": Unserializable()}
        serialized = safe_serialize(obj)
        assert isinstance(serialized, str)
        assert "123" in serialized
        assert "[UNSERIALIZABLE" in serialized

    def test_get_safe_error_metadata_extracts_clean_summary(self) -> None:
        register_secret("raw_secret_in_exception_msg")

        try:
            raise ConnectionError(
                "Failed connecting with raw_secret_in_exception_msg at host 10.0.0.1"
            )
        except Exception as exc:
            meta = get_safe_error_metadata(exc, component="test_http", operation="connect")

        assert meta["component"] == "test_http"
        assert meta["operation"] == "connect"
        assert meta["exception_class"] == "ConnectionError"
        assert "raw_secret_in_exception_msg" not in meta["safe_reason"]
        assert "[REDACTED]" in meta["safe_reason"]
        assert "location" in meta
        # Verify no locals or raw stack dumps leaked into metadata
        assert "locals" not in meta
