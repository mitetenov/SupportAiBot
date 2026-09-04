"""Credential redaction, object sanitization, and safe error metadata."""

import json
import re
import traceback
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import SecretStr

# Known sensitive dictionary / JSON keys (matched case-insensitively)
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "set-cookie",
        "webhook_secret",
        "client_secret",
        "private_key",
        "bot_token",
        "telegram_bot_token",
        "pgvector_password",
        "postgres_password",
        "remnawave_api_token",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "groq_api_key",
        "bedolaga_api_key",
        "bedolaga_webhook_secret",
    }
)

# Regular expressions for credential patterns
_URL_USERINFO_PATTERN = re.compile(r"://([^:]+):([^@]+)@")
_URL_QUERY_SENSITIVE_PATTERN = re.compile(
    r"(?i)(?<=[?&])(token|api_key|key|secret|password|auth|access_token)=([^&#\s]+)"
)
_TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
_OPENAI_API_KEY_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
_GROQ_API_KEY_PATTERN = re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b")
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"""(?i)(["']?Authorization["']?\s*:\s*["']?(?:Bearer|Basic)\s+)[^\r\n,;'"\}\]]+"""
)
_COOKIE_HEADER_PATTERN = re.compile(r"""(?i)(["']?Cookie["']?\s*:\s*["']?)[^\r\n'"\}\]]+""")
_PASSWORD_ASSIGNMENT_PATTERN = re.compile(
    r"""(?i)(["']?(?:password|passwd|secret|api_key|apikey|token|access_token|refresh_token|bot_token|webhook_secret)["']?)\s*([:=])\s*(['"][^'"]*['"]|[^'\"\s,&;\}]+)"""
)

# Dynamic set of registered known secret strings (e.g. from Settings)
_REGISTERED_SECRETS: set[str] = set()


def clear_registered_secrets() -> None:
    """Clear all registered secrets."""
    _REGISTERED_SECRETS.clear()


def register_secret(secret: str | SecretStr | None) -> None:
    """Register a known secret value to be scrubbed from text across all log levels."""
    if secret is None:
        return
    val = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
    val = val.strip()
    # Avoid registering empty or trivial strings that would over-redact
    if len(val) >= 4:
        _REGISTERED_SECRETS.add(val)


def register_settings_secrets(settings: Any) -> None:
    """Register all non-empty credentials found in the given Settings object."""
    if settings is None:
        return
    model_fields = getattr(settings.__class__, "model_fields", {})
    for field_name in model_fields:
        if any(w in field_name for w in ("token", "key", "password", "secret")):
            val = getattr(settings, field_name, None)
            register_secret(val)


def redact_credentials_in_text(text: str) -> str:
    """Sanitize credentials in a string while preserving non-credential text and PII."""
    if not isinstance(text, str) or not text:
        return text

    result = text

    # Redact URL credentials (user:password@host -> user:[REDACTED]@host)
    result = _URL_USERINFO_PATTERN.sub(r"://\1:[REDACTED]@", result)

    # Redact URL query parameters
    result = _URL_QUERY_SENSITIVE_PATTERN.sub(r"\1=[REDACTED]", result)

    # Redact Authorization headers
    result = _AUTHORIZATION_HEADER_PATTERN.sub(r"\1[REDACTED]", result)

    # Redact Cookie headers
    result = _COOKIE_HEADER_PATTERN.sub(r"\1[REDACTED]", result)

    # Redact Telegram bot tokens
    result = _TELEGRAM_BOT_TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", result)

    # Redact OpenAI API keys
    result = _OPENAI_API_KEY_PATTERN.sub("[REDACTED_API_KEY]", result)

    # Redact Groq API keys
    result = _GROQ_API_KEY_PATTERN.sub("[REDACTED_API_KEY]", result)

    def _redact_assignment(m: re.Match[str]) -> str:
        k = m.group(1)
        sep = m.group(2)
        v = m.group(3)
        if v.startswith(("'", '"')) and v.endswith(("'", '"')):
            q = v[0]
            return f"{k}{sep}{q}[REDACTED]{q}"
        return f"{k}{sep}[REDACTED]"

    result = _PASSWORD_ASSIGNMENT_PATTERN.sub(_redact_assignment, result)

    # Redact registered custom secrets (longest first to avoid partial collisions)
    for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, "[REDACTED]")

    return result


def is_sensitive_key(key: str) -> bool:
    """Check if a dictionary or attribute key indicates sensitive credential data."""
    str_key = str(key).lower()
    norm_key = str_key.replace("-", "_")
    return (
        str_key in SENSITIVE_KEYS
        or norm_key in SENSITIVE_KEYS
        or any(
            s in norm_key
            for s in ("password", "secret", "token", "api_key", "apikey", "auth", "cookie")
        )
    )


def redact_data(data: Any) -> Any:
    """Recursively redact credentials in mappings, sequences, and strings without mutating originals."""
    if data is None or isinstance(data, (int, float, bool)):
        return data

    if isinstance(data, SecretStr):
        return "[REDACTED]"

    if isinstance(data, str):
        return redact_credentials_in_text(data)

    if isinstance(data, Mapping):
        redacted_dict: dict[Any, Any] = {}
        for key, value in data.items():
            if is_sensitive_key(str(key)):
                redacted_dict[key] = "[REDACTED]"
            else:
                redacted_dict[key] = redact_data(value)
        return redacted_dict

    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return [redact_data(item) for item in data]

    if isinstance(data, set):
        return {redact_data(item) for item in data}

    # For other objects, safely convert to str or return unserializable marker
    try:
        s = str(data)
        return redact_credentials_in_text(s)
    except Exception:
        cls_name = data.__class__.__name__ if hasattr(data, "__class__") else "unknown"
        return f"[UNSERIALIZABLE: {cls_name}]"


def safe_serialize(obj: Any) -> str:
    """Safely serialize an object to JSON/str, falling back to a safe marker without crashing."""
    try:
        sanitized = redact_data(obj)
        return json.dumps(sanitized, ensure_ascii=False, default=str)
    except Exception:
        pass

    try:
        return redact_credentials_in_text(str(obj))
    except Exception:
        cls_name = obj.__class__.__name__ if hasattr(obj, "__class__") else "unknown"
        return f"[UNSERIALIZABLE: {cls_name}]"


def get_safe_error_metadata(
    exc: BaseException,
    component: str = "",
    operation: str = "",
) -> dict[str, Any]:
    """Generate safe, credential-free error metadata without leaking frame locals or code lines."""
    exc_class = exc.__class__.__name__
    # Exception messages are supplied by remote services and may contain user
    # text, SQL parameters, or response bodies.  The exception type and an
    # optional status code are enough for INFO/ERROR correlation; full text is
    # emitted only with the accompanying TRACE record.
    safe_reason = exc_class

    location = "unknown"
    tb = exc.__traceback__
    if tb is not None:
        frames = traceback.extract_tb(tb)
        if frames:
            last_frame = frames[-1]
            location = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}"

    metadata: dict[str, Any] = {
        "component": component,
        "operation": operation,
        "exception_class": exc_class,
        "safe_reason": safe_reason,
        "location": location,
    }

    status_code = getattr(exc, "status_code", getattr(exc, "code", None))
    if status_code is not None and isinstance(status_code, (int, str)):
        metadata["error_code"] = str(status_code)

    return metadata
