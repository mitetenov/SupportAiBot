"""Unit tests for RejectionDetector domain logic."""

import pytest

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
        assert is_rejection("Как подключить другое устройство?") is False
        assert is_rejection("Не только на телефоне") is False

    def test_rejection_detector_class_methods(self) -> None:
        assert RejectionDetector.is_rejection("это не то") is True
        assert RejectionDetector.is_rejection("не подходит") is True
        assert RejectionDetector.is_rejection("обычный вопрос") is False


class TestFollowUpRejections:
    """«Сделал как сказали — не изменилось» тоже отказ.

    Раньше такие формулировки отказом не считались, из-за чего набор уже
    показанных FAQ сбрасывался каждый ход, и бот предлагал ту же инструкцию
    по кругу вместо перехода к следующей.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "все равно не работает",
            "всё равно не работает",
            "всё ещё не работает",
            "по-прежнему не работает",
            "по прежнему не подключается",
            "ничего не изменилось",
            "ничего не поменялось",
            "ничего не помогает",
            "то же самое",
            "тоже самое",
            "опять не работает",
            "снова не работает",
            "также не работает",
            "так же не работает",
            "не заработало",
            "без изменений",
            "не помогло",
        ],
    )
    def test_recognised_as_rejection(self, message: str) -> None:
        assert is_rejection(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "не работает впн",
            "не могу подключиться",
            "не работает германия",
            "как оплатить подписку",
            "где скачать приложение",
            "сколько стоит",
            "верните деньги",
            "привет",
            "нужен оператор",
        ],
    )
    def test_first_complaint_is_not_a_rejection(self, message: str) -> None:
        assert is_rejection(message) is False

    def test_yo_and_e_are_the_same_phrase(self) -> None:
        assert is_rejection("всё ещё не работает") == is_rejection("все еще не работает")
