import io
import logging
import sys
from collections.abc import Iterator

import pytest

from app.logging_config import (
    TRACE,
    TRACE_LEVEL_NAME,
    TRACE_LEVEL_NUM,
    SafeConsoleFormatter,
    SafeConsoleHandler,
    escape_control_chars,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _restore_default_logging() -> Iterator[None]:
    yield
    setup_logging(level="INFO")


class TestTraceLevelDefinition:
    """Verify TRACE level registration below DEBUG."""

    def test_trace_level_is_below_debug(self) -> None:
        assert TRACE_LEVEL_NUM < logging.DEBUG
        assert TRACE_LEVEL_NUM == 5
        assert TRACE == 5
        assert logging.getLevelName(TRACE_LEVEL_NUM) == TRACE_LEVEL_NAME
        assert hasattr(logging.Logger, "trace")

    def test_logger_has_trace_method(self) -> None:
        logger = logging.getLogger("test_trace_method")
        assert hasattr(logger, "trace")


class TestControlCharacterEscaping:
    """Verify control characters and escape sequences are escaped against log forging."""

    def test_escape_newlines_and_carriage_returns(self) -> None:
        raw = "Line 1\nLine 2\r\n2026-09-04 [ERROR] fake_logger: Injected entry"
        escaped = escape_control_chars(raw)
        assert "\n" not in escaped
        assert "\r" not in escaped
        assert r"\n" in escaped
        assert r"\r" in escaped

    def test_escape_ansi_sequences_and_control_bytes(self) -> None:
        raw = "\x1b[31mRed text\x1b[0m\x00\x07alert"
        escaped = escape_control_chars(raw)
        assert "\x1b" not in escaped
        assert "\x00" not in escaped
        assert "\x07" not in escaped
        assert r"\x1b" in escaped or r"\x00" in escaped

    def test_preserve_normal_unicode_and_cyrillic(self) -> None:
        raw = "Привет, мир! VPN статус: отлично 🚀"
        escaped = escape_control_chars(raw)
        assert escaped == raw


class TestSafeConsoleFormatter:
    """Verify output formatting: UTC timestamp, canonical label, component, message."""

    def test_format_record_with_utc_and_canonical_label(self) -> None:
        formatter = SafeConsoleFormatter()
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="System started successfully",
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert "[INFO]" in formatted
        assert "app.test:" in formatted
        assert "System started successfully" in formatted
        assert "Z " in formatted or "UTC" in formatted

    def test_format_escapes_control_chars_in_message_and_fields(self) -> None:
        formatter = SafeConsoleFormatter()
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Untrusted payload:\n2026-09-04 [ERROR] Admin leaked\r\n",
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert "\n" not in formatted
        assert "\r" not in formatted
        assert r"\n" in formatted

    def test_format_canonical_labels_are_strictly_trace_info_error(self) -> None:
        formatter = SafeConsoleFormatter()
        for level, expected_label in [
            (TRACE_LEVEL_NUM, "[TRACE]"),
            (logging.DEBUG, "[TRACE]"),
            (logging.INFO, "[INFO]"),
            (logging.WARNING, "[INFO]"),
            (logging.ERROR, "[ERROR]"),
            (logging.CRITICAL, "[ERROR]"),
        ]:
            record = logging.LogRecord(
                name="app.test",
                level=level,
                pathname="test.py",
                lineno=10,
                msg="Sample msg",
                args=(),
                exc_info=None,
            )
            formatted = formatter.format(record)
            assert expected_label in formatted


class TestCumulativeThresholds:
    """Verify cumulative thresholds for TRACE, INFO, ERROR."""

    def test_trace_level_outputs_trace_info_and_error(self) -> None:
        stream = io.StringIO()
        setup_logging(level="TRACE", stream=stream)

        logger = logging.getLogger("app.threshold_test")
        logger.trace("A trace message")
        logger.info("An info message")
        logger.error("An error message")

        output = stream.getvalue()
        assert "A trace message" in output
        assert "An info message" in output
        assert "An error message" in output

    def test_info_level_outputs_info_and_error_but_not_trace(self) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        logger = logging.getLogger("app.threshold_test")
        logger.trace("Hidden trace message")
        logger.info("Visible info message")
        logger.error("Visible error message")

        output = stream.getvalue()
        assert "Hidden trace message" not in output
        assert "Visible info message" in output
        assert "Visible error message" in output

    def test_error_level_outputs_only_error(self) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        logger = logging.getLogger("app.threshold_test")
        logger.trace("Hidden trace message")
        logger.info("Hidden info message")
        logger.error("Visible error message")

        output = stream.getvalue()
        assert "Hidden trace message" not in output
        assert "Hidden info message" not in output
        assert "Visible error message" in output


class TestSetupIdempotenceAndDynamicReconfiguration:
    """Verify repeated setup does not duplicate handlers and reconfigures dynamically."""

    def test_repeated_setup_does_not_duplicate_handlers_or_output(self) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)
        setup_logging(level="INFO", stream=stream)

        root = logging.getLogger()
        console_handlers = [h for h in root.handlers if isinstance(h, SafeConsoleHandler)]
        assert len(console_handlers) == 1

        logger = logging.getLogger("app.idempotence")
        logger.info("Single line message")
        lines = [
            line for line in stream.getvalue().strip().splitlines() if "Single line message" in line
        ]
        assert len(lines) == 1

    def test_dynamic_reconfiguration_updates_threshold(self) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        logger = logging.getLogger("app.dynamic")
        logger.info("Should not appear in error mode")
        assert "Should not appear in error mode" not in stream.getvalue()

        # Reconfigure to INFO
        setup_logging(level="INFO", stream=stream)
        logger.info("Should appear in info mode")
        assert "Should appear in info mode" in stream.getvalue()


class TestDependencyNormalization:
    """Verify third-party loggers are normalized: DEBUG/INFO/WARNING -> TRACE, ERROR/CRIT -> ERROR."""

    def test_third_party_debug_info_warning_routed_to_trace(self) -> None:
        stream = io.StringIO()
        # In INFO mode, third-party debug/info/warning must not be output
        setup_logging(level="INFO", stream=stream)

        ext_logger = logging.getLogger("httpx")
        ext_logger.debug("HTTP Request: GET http://example.com")
        ext_logger.info("HTTP Request: POST http://example.com 200 OK")
        ext_logger.warning("HTTP retry attempt 1")

        assert "HTTP Request" not in stream.getvalue()
        assert "HTTP retry" not in stream.getvalue()

        # In TRACE mode, third-party records appear normalized with TRACE label
        stream_trace = io.StringIO()
        setup_logging(level="TRACE", stream=stream_trace)

        ext_logger.debug("HTTP Request: GET http://example.com/trace")
        output = stream_trace.getvalue()
        assert "HTTP Request: GET http://example.com/trace" in output
        assert "[TRACE]" in output

    def test_third_party_error_produces_safe_summary_on_error_level(self) -> None:
        stream = io.StringIO()
        setup_logging(level="ERROR", stream=stream)

        sql_logger = logging.getLogger("sqlalchemy.engine")
        sql_logger.error("SELECT * FROM users WHERE password='super_secret_db_pass_123'")

        output = stream.getvalue()
        assert "[ERROR]" in output
        # Password credentials must not leak
        assert "super_secret_db_pass_123" not in output


class TestFormattingAndSerializationErrorSafety:
    """Formatting errors must fail safely with an error marker to stderr without crashing."""

    def test_unformattable_record_fails_safely_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stderr_capture = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_capture)

        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)

        class BrokenRepr:
            def __repr__(self) -> str:
                raise RuntimeError("broken repr")

        logger = logging.getLogger("app.broken")
        # Log with an object whose formatting will fail
        logger.info("Test with broken object: %s", BrokenRepr())

        # Logging did not crash, safe fallback emitted or error marker to stderr
        err_out = stderr_capture.getvalue()
        stream_out = stream.getvalue()
        assert (
            "[LOGGING_FORMAT_ERROR]" in stream_out
            or "[LOGGING_ERROR]" in err_out
            or "Test with broken object" in stream_out
        )
