"""Unit tests for IllustrationSender."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.conversation_state import ConversationState
from app.bot.illustrations import IllustrationSender
from app.bot.sender import TelegramMessageSender
from app.rag.types import FaqContext, FaqResult


def _context(*images: str | None) -> FaqContext:
    results = [
        FaqResult(f"Вопрос {i}", "Ответ", 0.8, 0.03, image=img) for i, img in enumerate(images)
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

    message_id = await illustrations.send_first(100, 100, _context("happ-buttons.png"))

    assert message_id == 907
    assert sender.send_photo.await_args.args[1].name == "happ-buttons.png"


@pytest.mark.asyncio
async def test_ignores_pictures_named_below_the_top_hit() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert await illustrations.send_first(100, 100, _context(None, "happ-buttons.png")) is None
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_sends_nothing_when_retrieval_came_back_empty() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert await illustrations.send_first(100, 100, FaqContext.EMPTY) is None
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_sends_nothing_when_the_named_file_is_not_shipped() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())

    assert await illustrations.send_first(100, 100, _context("../../etc/passwd")) is None
    sender.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_send_the_same_picture_twice_in_one_conversation() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    await illustrations.send_first(100, 100, context)
    second = await illustrations.send_first(100, 100, context)

    assert second is None
    sender.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_picture_that_failed_to_send_is_not_marked_as_seen() -> None:
    sender = _sender(photo_message_id=None)
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    assert await illustrations.send_first(100, 100, context) is None

    sender.send_photo = AsyncMock(return_value=907)
    assert await illustrations.send_first(100, 100, context) == 907


@pytest.mark.asyncio
async def test_another_user_gets_the_picture_of_their_own() -> None:
    sender = _sender()
    illustrations = IllustrationSender(sender, ConversationState())
    context = _context("happ-buttons.png")

    await illustrations.send_first(100, 100, context)
    await illustrations.send_first(200, 200, context)

    assert sender.send_photo.await_count == 2
