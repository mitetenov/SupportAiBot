"""Unit tests for the operator /ask command."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.conversation_state import ConversationState
from app.bot.operator_ask import OperatorAskCommand
from app.bot.sender import TelegramMessageSender
from app.llm.base import LlmProcessingException, LlmReply
from app.rag.types import FaqContext, FaqResult

SUPPORT_GROUP_ID = -1001234567890
TOPIC_ID = 42
USER_ID = 100


def _illustrated_context(image: str | None) -> FaqContext:
    return FaqContext(
        text="FAQ...",
        results=[FaqResult("Где кнопка?", "Нажмите правую кнопку", 0.8, 0.03, image=image)],
        max_similarity=0.8,
        best_question="Где кнопка?",
    )


def _command(
    reply: LlmReply | None = None,
    error: Exception | None = None,
    conversation_state: ConversationState | None = None,
) -> tuple[OperatorAskCommand, MagicMock, MagicMock]:
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        side_effect=error,
        return_value=reply if reply is not None else LlmReply(text="Оплатить можно в боте"),
    )
    sender = MagicMock(spec=TelegramMessageSender)
    sender.send = AsyncMock()
    sender.send_to_topic = AsyncMock()
    sender.send_photo = AsyncMock(return_value=907)
    typing_indicator = MagicMock()
    typing_indicator.start = MagicMock(return_value=MagicMock())

    command = OperatorAskCommand(
        llm_client=llm_client,
        sender=sender,
        conversation_state=conversation_state or ConversationState(),
        typing_indicator=typing_indicator,
        support_group_chat_id=SUPPORT_GROUP_ID,
    )
    return command, sender, llm_client


class TestParse:
    """Only a real /ask is a command; everything else is the operator's own text."""

    def test_returns_the_query_after_the_command(self) -> None:
        assert OperatorAskCommand.parse("/ask как оплатить") == "как оплатить"

    def test_returns_an_empty_query_when_the_command_stands_alone(self) -> None:
        assert OperatorAskCommand.parse("/ask") == ""

    def test_treats_trailing_whitespace_as_no_query(self) -> None:
        assert OperatorAskCommand.parse("/ask   ") == ""

    def test_accepts_the_command_addressed_to_the_bot(self) -> None:
        assert OperatorAskCommand.parse("/ask@SupportBot как оплатить") == "как оплатить"

    def test_keeps_a_multiline_query_whole(self) -> None:
        assert (
            OperatorAskCommand.parse("/ask как оплатить\nс карты РФ") == "как оплатить\nс карты РФ"
        )

    def test_a_longer_word_starting_with_ask_is_not_the_command(self) -> None:
        assert OperatorAskCommand.parse("/asking что-то") is None

    def test_plain_operator_text_is_not_the_command(self) -> None:
        assert OperatorAskCommand.parse("сейчас посмотрю") is None

    def test_another_command_is_not_the_command(self) -> None:
        assert OperatorAskCommand.parse("/stats") is None


class TestAnswering:
    """A /ask answer reaches the user as an ordinary bot message."""

    @pytest.mark.asyncio
    async def test_sends_the_model_answer_to_the_user(self) -> None:
        command, sender, llm_client = _command(LlmReply(text="Оплатить можно в боте"))

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        llm_client.chat.assert_awaited_once_with("как оплатить", USER_ID)
        sender.send.assert_awaited_once_with(USER_ID, "Оплатить можно в боте")

    @pytest.mark.asyncio
    async def test_posts_a_copy_of_what_was_sent_into_the_topic(self) -> None:
        command, sender, _ = _command(LlmReply(text="Оплатить можно в боте"))

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        sender.send_to_topic.assert_awaited_once()
        chat_id, topic_id, text = sender.send_to_topic.await_args.args
        assert (chat_id, topic_id) == (SUPPORT_GROUP_ID, TOPIC_ID)
        assert "Оплатить можно в боте" in text

    @pytest.mark.asyncio
    async def test_keeps_the_bot_muted_for_the_next_user_message(self) -> None:
        state = ConversationState()
        command, _, _ = _command(conversation_state=state)

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        assert state.is_operator_recently_active(USER_ID) is True

    @pytest.mark.asyncio
    async def test_strips_the_escalation_marker_from_the_user_answer(self) -> None:
        command, sender, _ = _command(LlmReply(text="Оформим возврат. [ESCALATE]"))

        await command.handle(TOPIC_ID, USER_ID, "хочу вернуть деньги")

        assert "[ESCALATE]" not in sender.send.await_args.args[1]

    @pytest.mark.asyncio
    async def test_sends_the_faq_picture_after_the_answer(self) -> None:
        command, sender, _ = _command(
            LlmReply(
                text="Нажмите правую кнопку", faq_context=_illustrated_context("happ-buttons.png")
            )
        )
        order: list[str] = []
        sender.send = AsyncMock(side_effect=lambda *a, **k: order.append("text"))
        sender.send_photo = AsyncMock(side_effect=lambda *a, **k: order.append("photo") or 907)

        await command.handle(TOPIC_ID, USER_ID, "где кнопка обновить")

        assert order == ["text", "photo"]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_standard_notice_when_the_model_says_nothing(self) -> None:
        command, sender, _ = _command(LlmReply(text="   "))

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        assert sender.send.await_args.args[1].strip() != ""

    @pytest.mark.asyncio
    async def test_does_not_record_the_operator_query_as_the_users_own(self) -> None:
        state = ConversationState()
        command, _, _ = _command(conversation_state=state)

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        assert state.last_query(USER_ID) is None

    @pytest.mark.asyncio
    async def test_shows_the_typing_status_in_the_users_chat(self) -> None:
        command, _, _ = _command()
        session = command.typing_indicator.start.return_value

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        command.typing_indicator.start.assert_called_once_with(USER_ID)
        session.close.assert_called_once()


class TestFailures:
    """Nothing half-finished reaches the user."""

    @pytest.mark.asyncio
    async def test_an_empty_query_only_gets_the_operator_a_usage_hint(self) -> None:
        command, sender, llm_client = _command()

        await command.handle(TOPIC_ID, USER_ID, "")

        llm_client.chat.assert_not_awaited()
        sender.send.assert_not_awaited()
        sender.send_to_topic.assert_awaited_once()
        assert "/ask" in sender.send_to_topic.await_args.args[2]

    @pytest.mark.asyncio
    async def test_a_model_failure_is_reported_to_the_operator_not_the_user(self) -> None:
        command, sender, _ = _command(error=LlmProcessingException("upstream 500"))

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        sender.send.assert_not_awaited()
        sender.send_to_topic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_closes_the_typing_status(self) -> None:
        command, _, _ = _command(error=RuntimeError("boom"))
        session = command.typing_indicator.start.return_value

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_huge_provider_error_does_not_flood_the_topic(self) -> None:
        command, sender, _ = _command(error=RuntimeError("x" * 9000))

        await command.handle(TOPIC_ID, USER_ID, "как оплатить")

        assert len(sender.send_to_topic.await_args.args[2]) < 1000
