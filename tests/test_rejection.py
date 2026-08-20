"""Unit tests for RejectionDetector domain logic."""

from app.llm.rejection import RejectionDetector, is_rejection


class TestRejectionDetector:
    """Test user rejection phrase matching and negative cases."""

    def test_should_recognise_rejections(self) -> None:
        assert is_rejection("это не то") is True
        assert is_rejection("не подходит") is True
        assert is_rejection("не помогло") is True
        assert is_rejection("не помог") is True
        assert is_rejection("дайте другой вариант") is True
        assert is_rejection("нет, я про другое") is True
        assert is_rejection("не та инструкция") is True
        assert is_rejection("другое") is True
        assert is_rejection("не это") is True
        assert is_rejection("нет, не то") is True

    def test_case_insensitivity(self) -> None:
        assert is_rejection("НЕ ПОДХОДИТ") is True
        assert is_rejection("Не То") is True
        assert is_rejection("ДРУГОЙ ВАРИАНТ") is True

    def test_should_not_recognise_ordinary_questions(self) -> None:
        assert is_rejection("как оплатить подписку") is False
        assert is_rejection("не работает VPN") is False
        assert is_rejection(None) is False
        assert is_rejection("") is False
        assert is_rejection("   ") is False
        assert is_rejection("подскажите настройки для роутера") is False

    def test_rejection_detector_class_methods(self) -> None:
        assert RejectionDetector.is_rejection("это не то") is True
        assert RejectionDetector.is_rejection("не подходит") is True
        assert RejectionDetector.is_rejection("обычный вопрос") is False
