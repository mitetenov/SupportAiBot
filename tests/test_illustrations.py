"""Unit tests for IllustrationSender."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.conversation_state import ConversationState
from app.bot.illustrations import IllustrationSender
from app.bot.sender import TelegramMessageSender
from app.rag.types import FaqContext, FaqResult

ILLUSTRATED_ANSWER = "Нажмите «Обновить подписку», затем «Пинг»."


def _context(*images: str | None) -> FaqContext:
    results = [
        FaqResult(f"Вопрос {i}", ILLUSTRATED_ANSWER, 0.8, 0.03, image=img)
        for i, img in enumerate(images)
    ]
    return FaqContext(text="FAQ...", results=results, max_similarity=0.8, best_question="Вопрос 0")


def _sender(photo_message_id: int | None = 907) -> MagicMock:
    sender = MagicMock(spec=TelegramMessageSender)
    sender.send_photo = AsyncMock(return_value=photo_message_id)
    return sender


@pytest.mark.asyncio
async def test_sends_the_picture_named_by_the_top_hit() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    message_id = await illustrations.send_first(
        100, 100, _context("happ-buttons.png"), ILLUSTRATED_ANSWER
    )

    assert message_id == 907
    assert sender.send_photo.await_args.args[1].name == "happ-buttons.png"


@pytest.mark.asyncio
async def test_ignores_pictures_named_below_the_top_hit() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert (
        await illustrations.send_first(
            100, 100, _context(None, "happ-buttons.png"), ILLUSTRATED_ANSWER
        )
        is None
    )
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_sends_nothing_when_retrieval_came_back_empty() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert await illustrations.send_first(100, 100, FaqContext.EMPTY, ILLUSTRATED_ANSWER) is None
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_sends_nothing_when_the_named_file_is_not_shipped() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert (
        await illustrations.send_first(100, 100, _context("../../etc/passwd"), ILLUSTRATED_ANSWER)
        is None
    )
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_send_the_same_picture_twice_in_one_conversation() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    await illustrations.send_first(100, 100, context, ILLUSTRATED_ANSWER)
    second = await illustrations.send_first(100, 100, context, ILLUSTRATED_ANSWER)

    assert second is None
    sender.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_picture_that_failed_to_send_is_not_marked_as_seen() -> None:
    sender = _sender(photo_message_id=None)
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    assert await illustrations.send_first(100, 100, context, ILLUSTRATED_ANSWER) is None

    sender.send_photo = AsyncMock(return_value=907)
    assert await illustrations.send_first(100, 100, context, ILLUSTRATED_ANSWER) == 907


@pytest.mark.asyncio
async def test_another_user_gets_the_picture_of_their_own() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    await illustrations.send_first(100, 100, context, ILLUSTRATED_ANSWER)
    await illustrations.send_first(200, 200, context, ILLUSTRATED_ANSWER)

    assert sender.send_photo.await_count == 2


@pytest.mark.asyncio
async def test_does_not_send_a_retrieved_picture_for_an_unrelated_tool_answer() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    message_id = await illustrations.send_first(
        100,
        100,
        _context("happ-buttons.png"),
        "Ваш текущий баланс: **10 рублей**.",
    )

    assert message_id is None
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_matches_an_instruction_despite_case_and_whitespace_changes() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    message_id = await illustrations.send_first(
        100,
        100,
        _context("happ-buttons.png"),
        "Инструкция:\n  НАЖМИТЕ «ОБНОВИТЬ ПОДПИСКУ», затем «Пинг».  ",
    )

    assert message_id == 907


@pytest.mark.asyncio
async def test_does_not_send_picture_for_summarized_or_partial_faq_answer() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    summarized_answer = "Обновите подписку и выполните пинг в приложении."
    message_id = await illustrations.send_first(100, 100, context, summarized_answer)

    assert message_id is None
    sender.send_photo.assert_not_awaited()
