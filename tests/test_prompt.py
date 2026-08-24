"""Unit tests for SupportPrompt system prompt builder and formatting."""

from app.llm.prompt import SupportPrompt


class TestSupportPrompt:
    """Validate system prompt content and Telegram ID/FAQ formatting."""

    def test_system_should_not_be_empty(self) -> None:
        assert SupportPrompt.SYSTEM is not None
        assert len(SupportPrompt.SYSTEM) > 0

    def test_system_should_contain_escalate_marker(self) -> None:
        assert "[ESCALATE]" in SupportPrompt.SYSTEM

    def test_system_should_send_installation_questions_to_ready_made_instruction(self) -> None:
        assert "не объясняй установку вне готовой инструкции" in SupportPrompt.SYSTEM
        assert "«Подключиться»" in SupportPrompt.SYSTEM
        assert "«Подключить устройство»" in SupportPrompt.SYSTEM
        assert "@PeipivoSalesBot" in SupportPrompt.SYSTEM
        assert "https://lk.peipivo.top" in SupportPrompt.SYSTEM

    def test_the_connection_rule_should_cover_additional_devices_too(self) -> None:
        assert "ПОДКЛЮЧЕНИЕ УСТРОЙСТВА" in SupportPrompt.SYSTEM
        assert "ПЕРВОГО устройства и для ЛЮБОГО СЛЕДУЮЩЕГО" in SupportPrompt.SYSTEM
        assert "под тем же аккаунтом" in SupportPrompt.SYSTEM

    def test_system_should_still_allow_troubleshooting_for_already_connected_users(self) -> None:
        assert "Правило не отменяет диагностику" in SupportPrompt.SYSTEM

    def test_bedolaga_tariff_fields_should_not_be_treated_as_panel_state(self) -> None:
        assert "tariff_id" in SupportPrompt.SYSTEM
        assert "tariff_name" in SupportPrompt.SYSTEM
        assert "bot_record_status" in SupportPrompt.SYSTEM
        assert "bot_record_effective_status" in SupportPrompt.SYSTEM
        assert "не подтверждают фактическую активность подписки" in SupportPrompt.SYSTEM

    def test_with_telegram_user_id_should_append_id(self) -> None:
        result = SupportPrompt.with_telegram_user_id(12345)
        assert result is not None
        assert "Telegram ID: 12345" in result
        assert result.startswith(SupportPrompt.SYSTEM)

    def test_with_faq_context_should_include_context_and_id(self) -> None:
        context = "FAQ: Как настроить VPN..."
        result = SupportPrompt.with_faq_context(context, 67890)
        assert result is not None
        assert "FAQ: Как настроить VPN..." in result
        assert "Telegram ID: 67890" in result

    def test_with_faq_context_null_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context(None, 123)
        assert result is not None
        assert "Telegram ID: 123" in result
        assert result.endswith("Telegram ID: 123")

    def test_with_faq_context_empty_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context("", 456)
        assert result is not None
        assert "Telegram ID: 456" in result

    def test_with_faq_context_blank_should_not_include_faq(self) -> None:
        result = SupportPrompt.with_faq_context("   ", 789)
        assert result is not None
        assert "Telegram ID: 789" in result

    def test_with_telegram_user_id_should_handle_negative_id(self) -> None:
        result = SupportPrompt.with_telegram_user_id(-1)
        assert result is not None
        assert "Telegram ID: -1" in result

    def test_system_should_mention_happ_and_incy(self) -> None:
        assert "Happ" in SupportPrompt.SYSTEM
        assert "Incy" in SupportPrompt.SYSTEM
        assert "n/a" in SupportPrompt.SYSTEM
        assert "несовместим" in SupportPrompt.SYSTEM
