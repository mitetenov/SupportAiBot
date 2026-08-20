"""Unit tests for EscalationPolicy domain logic."""

from app.llm.escalation import EscalationPolicy


class TestEscalationPolicy:
    """Test model escalation marker detection and user human-request regex parsing."""

    def test_model_requested_escalation_detection(self) -> None:
        assert EscalationPolicy.model_requested_escalation("Оформим возврат. [ESCALATE]") is True
        assert EscalationPolicy.model_requested_escalation("Обычный ответ") is False
        assert EscalationPolicy.model_requested_escalation(None) is False
        assert EscalationPolicy.model_requested_escalation("") is False
        assert EscalationPolicy.model_requested_escalation("[ESCALATE]") is True

    def test_strip_marker_and_whitespace(self) -> None:
        assert EscalationPolicy.strip_marker("Оформим возврат. [ESCALATE]") == "Оформим возврат."
        assert EscalationPolicy.strip_marker("[ESCALATE]") == ""
        assert EscalationPolicy.strip_marker(None) == ""
        assert EscalationPolicy.strip_marker("   [ESCALATE]   ") == ""
        assert (
            EscalationPolicy.strip_marker("Ответ перед маркером [ESCALATE] и после")
            == "Ответ перед маркером  и после"
        )

    def test_user_requests_human_explicit(self) -> None:
        assert EscalationPolicy.user_requests_human("позовите оператора") is True
        assert EscalationPolicy.user_requests_human("хочу поговорить с человеком") is True
        assert EscalationPolicy.user_requests_human("дайте живого человека") is True
        assert EscalationPolicy.user_requests_human("ОПЕРАТОР") is True
        assert EscalationPolicy.user_requests_human("человека мне") is True
        assert EscalationPolicy.user_requests_human("позови человека") is True
        assert EscalationPolicy.user_requests_human("нужен живой оператор") is True
        assert EscalationPolicy.user_requests_human("поговорить с живым") is True
        assert EscalationPolicy.user_requests_human("переключи на оператора") is True

    def test_user_requests_human_does_not_fire_on_subwords(self) -> None:
        # Prevent false positives on words containing root substrings
        assert EscalationPolicy.user_requests_human("я живу в Германии") is False
        assert EscalationPolicy.user_requests_human("болит живот") is False
        assert EscalationPolicy.user_requests_human("сайт оживает через раз") is False
        assert EscalationPolicy.user_requests_human("проживаю в РФ") is False
        assert EscalationPolicy.user_requests_human("я переживаю за подписку") is False

    def test_user_requests_human_empty_or_none(self) -> None:
        assert EscalationPolicy.user_requests_human(None) is False
        assert EscalationPolicy.user_requests_human("") is False
        assert EscalationPolicy.user_requests_human("   ") is False

    def test_camel_case_method_aliases(self) -> None:
        assert EscalationPolicy.modelRequestedEscalation("текст [ESCALATE]") is True
        assert EscalationPolicy.stripMarker("ответ [ESCALATE]") == "ответ"
        assert EscalationPolicy.userRequestsHuman("позовите оператора") is True
