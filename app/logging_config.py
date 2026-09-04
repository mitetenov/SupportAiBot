"""Unified logging configuration, TRACE level definition, safe formatting, and dependency normalization."""

import datetime
import logging
import sys
from typing import Any, TextIO

from app.logging_context import get_logging_context
from app.logging_redaction import redact_credentials_in_text

TRACE_LEVEL_NUM: int = 5
TRACE_LEVEL_NAME: str = "TRACE"
TRACE: int = TRACE_LEVEL_NUM

# Register TRACE level in standard logging
logging.addLevelName(TRACE_LEVEL_NUM, TRACE_LEVEL_NAME)
setattr(logging, TRACE_LEVEL_NAME, TRACE_LEVEL_NUM)


def _trace(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kwargs)


if not hasattr(logging.Logger, "trace"):
    setattr(logging.Logger, "trace", _trace)  # noqa: B010


def escape_control_chars(text: str) -> str:
    """Escape control characters and escape sequences to prevent log injection (CWE-117)."""
    if not isinstance(text, str):
        text = str(text)
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append(r"\n")
        elif ch == "\r":
            out.append(r"\r")
        elif ch == "\t":
            out.append(r"\t")
        elif code < 32 or code == 127:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def get_canonical_label(levelno: int) -> str:
    """Map standard and third-party log levels strictly to TRACE, INFO, or ERROR."""
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.INFO:
        return "INFO"
    return "TRACE"


class SafeConsoleFormatter(logging.Formatter):
    """Single readable console format: UTC timestamp, canonical label, component, message, context."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            # 1. UTC Timestamp (ISO 8601 with Z)
            dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
            timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            # 2. Canonical level label
            canonical_label = get_canonical_label(record.levelno)

            # 3. Component / Logger name
            component = record.name

            # 4. Format and redact raw message
            raw_msg = record.getMessage()
            sanitized_msg = redact_credentials_in_text(raw_msg)
            escaped_msg = escape_control_chars(sanitized_msg)

            # 5. Diagnostic / context fields
            ctx_parts: list[str] = []

            # Event name if provided
            event = getattr(record, "event", None)
            if event:
                escaped_event = escape_control_chars(str(event))
                ctx_parts.append(f"event={escaped_event}")

            # Async context fields (correlation_id, request_id, attempt_id)
            ctx = get_logging_context()
            for k in ("correlation_id", "request_id", "attempt_id"):
                if k in ctx:
                    ctx_parts.append(f"{k}={ctx[k]}")

            ctx_str = f" [{' '.join(ctx_parts)}]" if ctx_parts else ""

            line = f"{timestamp} [{canonical_label}] {component}: {escaped_msg}{ctx_str}"

            # 6. Format exceptions if present (e.g. detailed TRACE or ERROR)
            if record.exc_info:
                try:
                    exc_text = self.formatException(record.exc_info)
                    sanitized_exc = redact_credentials_in_text(exc_text)
                    escaped_exc = escape_control_chars(sanitized_exc)
                    line = f"{line} | exc={escaped_exc}"
                except Exception:
                    line = f"{line} | exc=[EXC_FORMAT_ERROR]"

            return line
        except Exception:
            # Formatting must fail safely without crashing or leaking secrets
            try:
                sys.stderr.write("[LOGGING_FORMAT_ERROR] Failed to format log record\n")
            except Exception:
                pass
            dt_fallback = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return f"{dt_fallback} [ERROR] {record.name}: [LOGGING_FORMAT_ERROR]"


class SafeConsoleHandler(logging.StreamHandler):
    """Console stream handler that avoids leaking raw LogRecords to stderr on error."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream=stream or sys.stdout)
        self.setFormatter(SafeConsoleFormatter())

    def handleError(self, record: logging.LogRecord) -> None:
        """Override standard handleError to prevent dumping unredacted LogRecord to stderr."""
        try:
            sys.stderr.write("[LOGGING_ERROR] Failed to emit log record\n")
        except Exception:
            pass


class NormalizingFilter(logging.Filter):
    """Filter that normalizes third-party loggers and enforces cumulative log thresholds."""

    def __init__(self, bot_log_level: str) -> None:
        super().__init__()
        self.set_level(bot_log_level)

    def set_level(self, bot_log_level: str) -> None:
        level_str = (bot_log_level or "INFO").strip().upper()
        if level_str == "TRACE":
            self.min_levelno = TRACE_LEVEL_NUM
        elif level_str == "ERROR":
            self.min_levelno = logging.ERROR
        else:
            self.min_levelno = logging.INFO
        self.bot_log_level = level_str

    def filter(self, record: logging.LogRecord) -> bool:
        # Check and safely format args if any arg has a broken repr/str
        if record.args:
            try:
                _ = str(record.msg) % record.args
            except Exception:
                safe_args: list[Any] = []
                raw_args = record.args if isinstance(record.args, tuple) else (record.args,)
                for a in raw_args:
                    try:
                        _ = str(a)
                        safe_args.append(a)
                    except Exception:
                        safe_args.append(f"[{a.__class__.__name__}]")
                record.args = tuple(safe_args)

        # Route third-party loggers
        is_app = record.name == "app" or record.name.startswith("app.")
        if not is_app and record.name != "root":
            if record.levelno < logging.ERROR:
                # Route third-party DEBUG, INFO, WARNING to TRACE
                record.levelno = TRACE_LEVEL_NUM
                record.levelname = TRACE_LEVEL_NAME
            else:
                # Route third-party ERROR, CRITICAL to ERROR
                record.levelno = logging.ERROR
                record.levelname = "ERROR"
                # Redact raw messages
                if isinstance(record.msg, str):
                    record.msg = redact_credentials_in_text(record.msg)

        # Cumulative threshold check
        return record.levelno >= self.min_levelno


_CURRENT_FILTER: NormalizingFilter | None = None
_CURRENT_HANDLER: SafeConsoleHandler | None = None

THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "aiogram",
    "aiohttp",
    "aiohttp.access",
    "aiohttp.client",
    "aiohttp.server",
    "aiohttp.web",
    "sqlalchemy",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "sqlalchemy.dialects",
    "asyncpg",
    "mcp",
    "urllib3",
)


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure unified logging with canonical levels TRACE, INFO, ERROR.

    Idempotent: Re-running updates thresholds and routing without duplicating handlers.
    """
    global _CURRENT_FILTER, _CURRENT_HANDLER

    normalized_level = (level or "INFO").strip().upper()
    if normalized_level not in ("TRACE", "INFO", "ERROR"):
        normalized_level = "INFO"

    root = logging.getLogger()
    # Ensure root allows TRACE records through to the handler
    root.setLevel(TRACE_LEVEL_NUM)

    # Find or create SafeConsoleHandler
    handler: SafeConsoleHandler | None = None
    for h in list(root.handlers):
        if isinstance(h, SafeConsoleHandler):
            handler = h
            if stream is not None and h.stream != stream:
                h.setStream(stream)
            break

    if handler is None:
        # Remove any default handlers (e.g. from basicConfig)
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = SafeConsoleHandler(stream=stream)
        root.addHandler(handler)

    _CURRENT_HANDLER = handler

    # Update or install NormalizingFilter
    if _CURRENT_FILTER is None:
        _CURRENT_FILTER = NormalizingFilter(normalized_level)
        handler.addFilter(_CURRENT_FILTER)
        root.addFilter(_CURRENT_FILTER)
    else:
        _CURRENT_FILTER.set_level(normalized_level)
        if _CURRENT_FILTER not in root.filters:
            root.addFilter(_CURRENT_FILTER)

    # Configure third-party loggers: clear unmanaged handlers and ensure propagation
    all_logger_names = set(THIRD_PARTY_LOGGERS)
    for name in list(logging.root.manager.loggerDict.keys()):
        if not (name == "app" or name.startswith("app.")):
            all_logger_names.add(name)

    for name in all_logger_names:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        if normalized_level == "TRACE":
            logger.setLevel(TRACE_LEVEL_NUM)
        elif normalized_level == "ERROR":
            logger.setLevel(logging.ERROR)
        else:
            if name == "mcp":
                logger.setLevel(logging.WARNING)
            else:
                logger.setLevel(logging.INFO)
