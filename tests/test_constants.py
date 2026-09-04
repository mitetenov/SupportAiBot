"""Tests for constants, messages, support prompt formatting, and regex patterns."""

from app.constants import (
    MESSAGES,
    EscalationRegexes,
    SupportPrompt,
    get_message,
)


class TestMessages:
    """Test message retrieval and string formatting."""

    def test_should_retrieve_plain_message(self) -> None:
        assert get_message("bot.start.welcome") == "Привет, чем вам помочь? Опишите проблему."

    def test_should_substitute_positional_arguments(self) -> None:
        assert get_message("support.operator.prefix", "привет") == "Поддержка: привет"

    def test_should_substitute_stats_message_arguments(self) -> None:
        text = get_message("bot.stats.user", "@johndoe", 5, "1.0K", "500", "1.5K")
        assert "@johndoe" in text
        assert "1.5K" in text
        assert "{" not in text

    def test_should_fallback_to_key_if_not_found(self) -> None:
        assert get_message("nonexistent.key") == "nonexistent.key"

    def test_messages_dictionary_completeness(self) -> None:
        required_keys = [
            "bot.start.welcome",
            "bot.help",
            "bot.operator.transfer",
            "bot.unknown.command",
            "bot.ratelimit.wait",
            "bot.llm.error",
            "bot.llm.empty",
            "bot.media.unsupported",
            "bot.photo.notsupported",
            "bot.photo.download.error",
            "bot.photo.upload.error",
            "bot.photo.error",
            "bot.photo.default.prompt",
            "bot.stats.empty",
            "bot.stats.no.data",
            "bot.stats.top.header",
            "bot.stats.top.row",
            "bot.stats.user",
            "bot.gaps.empty",
            "bot.gaps.header",
            "bot.gaps.row",
            "support.operator.prefix",
            "support.sent",
            "support.fallback",
            "support.fallback.media",
            "support.operator.request",
            "support.media.received",
            "support.ratelimited",
            "support.ai.suppressed",
            "support.cabinet_only.no_delivery",
            "admin.error.prefix",
            "admin.error.details",
            "admin.response.prefix",
            "admin.response.truncated",
            "bedolaga.llm.empty",
            "bedolaga.escalation.note",
            "bedolaga.mirror",
            "bedolaga.nothing.to.answer",
            "bedolaga.nothing.mirror",
            "bedolaga.media.caption",
            "bedolaga.media.forward.failed",
        ]
        for key in required_keys:
            assert key in MESSAGES, f"Missing key in MESSAGES: {key}"
            assert MESSAGES[key].strip() != ""


class TestSupportPrompt:
    """Test SupportPrompt template and contextual formatting."""

    def test_system_should_not_be_empty(self) -> None:
        assert SupportPrompt.SYSTEM is not None
        assert len(SupportPrompt.SYSTEM) > 0

    def test_system_should_contain_escalate_marker(self) -> None:
        assert "[ESCALATE]" in SupportPrompt.SYSTEM

    def test_system_should_send_installation_questions_to_ready_made_instruction(self) -> None:
        assert "никогда не объясняй сам" in SupportPrompt.SYSTEM
        assert "«Подключиться»" in SupportPrompt.SYSTEM
        assert "«Подключить устройство»" in SupportPrompt.SYSTEM

    def test_the_connection_rule_should_cover_additional_devices_too(self) -> None:
        assert "ПОДКЛЮЧЕНИЕ УСТРОЙСТВА" in SupportPrompt.SYSTEM
        assert "ПЕРВОГО устройства и для ЛЮБОГО СЛЕДУЮЩЕГО" in SupportPrompt.SYSTEM
        assert "под тем же аккаунтом" in SupportPrompt.SYSTEM

    def test_system_should_still_allow_troubleshooting_for_already_connected_users(self) -> None:
        assert "Правило не отменяет диагностику" in SupportPrompt.SYSTEM

    def test_system_should_require_a_fresh_account_scoped_hwid_snapshot(self) -> None:
        prompt = SupportPrompt.SYSTEM
        assert "этот support-инструмент сам получает все аккаунты" in prompt
        assert "Не используй старые userId/hwid из истории" in prompt
        assert "status=already_absent" in prompt

    def test_with_telegram_user_id_should_append_id(self) -> None:
        result = SupportPrompt.with_telegram_user_id(12345)
        assert "Telegram ID: 12345" in result
        assert result.startswith(SupportPrompt.SYSTEM)

    def test_with_faq_context_should_include_context_and_id(self) -> None:
        context = "FAQ: Как настроить VPN..."
        result = SupportPrompt.with_faq_context(context, 67890)
        assert "FAQ: Как настроить VPN..." in result
        assert "Telegram ID: 67890" in result

    def test_with_faq_context_null_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context(None, 123)
        assert "Telegram ID: 123" in result
        assert "None" not in result

    def test_with_faq_context_empty_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context("", 456)
        assert "Telegram ID: 456" in result

    def test_with_faq_context_blank_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context("   ", 789)
        assert "Telegram ID: 789" in result

    def test_with_telegram_user_id_should_handle_negative_id(self) -> None:
        result = SupportPrompt.with_telegram_user_id(-1)
        assert "Telegram ID: -1" in result

    def test_system_should_mention_happ_and_incy(self) -> None:
        assert "Happ" in SupportPrompt.SYSTEM
        assert "Incy" in SupportPrompt.SYSTEM
        assert "n/a" in SupportPrompt.SYSTEM
        assert "несовместим" in SupportPrompt.SYSTEM


class TestEscalationRegexes:
    """Test regex pattern matching and word boundaries."""

    def test_asks_for_human_matches(self) -> None:
        for phrase in [
            "позовите оператора",
            "хочу поговорить с человеком",
            "дайте живого человека",
            "ОПЕРАТОР",
            "позови живого оператора",
            "нужен живой человек",
        ]:
            assert EscalationRegexes.ASKS_FOR_HUMAN.search(phrase) is not None, (
                f"Failed on {phrase}"
            )

    def test_asks_for_human_does_not_match_subwords(self) -> None:
        for phrase in [
            "я живу в Германии",
            "болит живот",
            "сайт оживает через раз",
        ]:
            assert EscalationRegexes.ASKS_FOR_HUMAN.search(phrase) is None, (
                f"Matched falsely on {phrase}"
            )
