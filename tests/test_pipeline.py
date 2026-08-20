"""Unit tests for UserMessagePipeline."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.llm.base import LlmProcessingException, LlmReply


class DummyChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class DummyUser:
    def __init__(self, user_id: int):
        self.id = user_id


class DummyMessage:
    def __init__(self, message_id: int, user_id: int = 100):
        self.message_id = message_id
        self.chat = DummyChat(user_id)
        self.from_user = DummyUser(user_id)

    @property
    def user(self) -> DummyUser:
        return self.from_user


def make_batch(
    text: str,
    user_id: int = 100,
    message_ids: list[int] | None = None,
    base64_image: str | None = None,
    mime_type: str | None = None,
) -> MessageBatch:
    msg = DummyMessage(message_ids[0] if message_ids else 1, user_id)
    return MessageBatch(
        last_message=msg,
        user=DummyUser(user_id),
        text=text,
        message_ids=message_ids or [1],
        base64_image=base64_image,
        mime_type=mime_type,
    )


@pytest.mark.asyncio
async def test_pipeline_normal_flow():
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=LlmReply(text="Попробуйте обновить подписку"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    rate_limiter = UserRateLimiter(min_interval=3.0)
    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()

    conv_state = ConversationState()
    typing_indicator = MagicMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        bot=bot,
        forwarder=forwarder,
        rate_limiter=rate_limiter,
        knowledge_gap_service=gap_service,
        conversation_state=conv_state,
        typing_indicator=typing_indicator,
    )

    batch = make_batch("не работает впн", user_id=100)
    await pipeline.handle(batch)

    llm_client.chat.assert_called_once_with("не работает впн", 100)
    bot.send_message.assert_called_once_with(chat_id=100, text="Попробуйте обновить подписку")
    forwarder.forward_to_support.assert_called_once_with(
        100, [1], batch.user, "Попробуйте обновить подписку", False
    )
    gap_service.evaluate.assert_called_once()
    assert conv_state.last_query(100).text == "не работает впн"


@pytest.mark.asyncio
async def test_pipeline_strips_escalate_and_flags_admin():
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=LlmReply(text="Оформим возврат средств. [ESCALATE]"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        bot=bot,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0),
        knowledge_gap_service=MagicMock(evaluate=AsyncMock()),
        conversation_state=ConversationState(),
        typing_indicator=MagicMock(),
    )

    batch = make_batch("хочу возврат", user_id=100)
    await pipeline.handle(batch)

    bot.send_message.assert_called_once_with(chat_id=100, text="Оформим возврат средств.")
    forwarder.forward_to_support.assert_called_once_with(
        100, [1], batch.user, "Оформим возврат средств.", True
    )


@pytest.mark.asyncio
async def test_pipeline_rate_limited():
    llm_client = MagicMock()
    llm_client.chat = AsyncMock()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    rate_limiter = MagicMock()
    rate_limiter.try_acquire = MagicMock(return_value=False)

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        bot=bot,
        forwarder=forwarder,
        rate_limiter=rate_limiter,
        knowledge_gap_service=MagicMock(),
        conversation_state=ConversationState(),
        typing_indicator=MagicMock(),
    )

    batch = make_batch("быстрый вопрос", user_id=100)
    await pipeline.handle(batch)

    llm_client.chat.assert_not_called()
    assert "быстрее" in bot.send_message.call_args[1]["text"]
    forwarder.forward_to_support.assert_called_once()
    assert forwarder.forward_to_support.call_args[0][4] is True  # escalate = True


@pytest.mark.asyncio
async def test_pipeline_operator_suppression():
    conv_state = ConversationState()
    conv_state.record_operator_reply(100)

    llm_client = MagicMock()
    llm_client.chat = AsyncMock()

    bot = MagicMock()
    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        bot=bot,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(),
        knowledge_gap_service=MagicMock(),
        conversation_state=conv_state,
        typing_indicator=MagicMock(),
    )

    batch = make_batch("ещё один вопрос", user_id=100)
    await pipeline.handle(batch)

    llm_client.chat.assert_not_called()
    bot.send_message.assert_not_called()
    forwarder.forward_to_support.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_handles_llm_processing_exception():
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        side_effect=LlmProcessingException("API timeout", "Сервис временно недоступен")
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_error_to_topic = AsyncMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        bot=bot,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0),
        knowledge_gap_service=MagicMock(),
        conversation_state=ConversationState(),
        typing_indicator=MagicMock(),
    )

    batch = make_batch("сложный вопрос", user_id=100)
    await pipeline.handle(batch)

    bot.send_message.assert_called_once_with(chat_id=100, text="Сервис временно недоступен")
    forwarder.forward_error_to_topic.assert_called_once()
