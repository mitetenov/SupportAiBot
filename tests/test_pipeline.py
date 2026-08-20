"""Unit tests for UserMessagePipeline."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.buffer import MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.sender import TelegramMessageSender
from app.llm.base import LlmProcessingException, LlmReply
from app.rag.types import FaqContext, FaqResult


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
    user_text: str | None = None,
) -> MessageBatch:
    msg = DummyMessage(message_ids[0] if message_ids else 1, user_id)
    return MessageBatch(
        last_message=msg,
        user=DummyUser(user_id),
        text=text,
        message_ids=message_ids or [1],
        base64_image=base64_image,
        mime_type=mime_type,
        user_text=text if user_text is None else user_text,
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
        sender=TelegramMessageSender(bot),
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
        100, [1], batch.user, "Попробуйте обновить подписку", False, illustration_message_id=None
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
        sender=TelegramMessageSender(bot),
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
        100, [1], batch.user, "Оформим возврат средств.", True, illustration_message_id=None
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
        sender=TelegramMessageSender(bot),
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
        sender=TelegramMessageSender(bot),
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
        sender=TelegramMessageSender(bot),
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


@pytest.mark.asyncio
async def test_one_turn_per_user_at_a_time():
    """Two batches for the same user must not be answered concurrently.

    Both turns read the chat history, call the model and append to it. Run at
    the same time they interleave those writes, and the next turn is built from
    a conversation that never happened.
    """
    in_flight = 0
    peak = 0

    async def chat(_text, _user_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return LlmReply(text="ответ")

    llm_client = MagicMock()
    llm_client.chat = chat

    bot = MagicMock()
    bot.send_message = AsyncMock()
    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()
    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        sender=TelegramMessageSender(bot),
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0.0),
        knowledge_gap_service=gap_service,
        conversation_state=ConversationState(),
        typing_indicator=MagicMock(),
    )

    await asyncio.gather(
        pipeline.handle(make_batch("первый вопрос", user_id=100)),
        pipeline.handle(make_batch("второй вопрос", user_id=100)),
    )

    assert peak == 1, "two turns for one user overlapped"


@pytest.mark.asyncio
async def test_turns_for_different_users_still_run_together():
    in_flight = 0
    peak = 0

    async def chat(_text, _user_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return LlmReply(text="ответ")

    llm_client = MagicMock()
    llm_client.chat = chat

    bot = MagicMock()
    bot.send_message = AsyncMock()
    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()
    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        sender=TelegramMessageSender(bot),
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0.0),
        knowledge_gap_service=gap_service,
        conversation_state=ConversationState(),
        typing_indicator=MagicMock(),
    )

    await asyncio.gather(
        pipeline.handle(make_batch("вопрос", user_id=100)),
        pipeline.handle(make_batch("вопрос", user_id=200)),
    )

    assert peak == 2, "one user's turn blocked another user's"


@pytest.mark.asyncio
async def test_should_not_record_a_knowledge_gap_for_a_captionless_screenshot():
    """The bot's own photo prompt is not a question anyone asked.

    Recorded as a gap it can never match the FAQ, so it climbs the /gaps report
    and displaces the questions users actually typed.
    """
    llm_client = MagicMock()
    llm_client.chat_with_image = AsyncMock(return_value=LlmReply(text="Вижу ошибку подключения"))

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()

    sender = MagicMock(spec=TelegramMessageSender)
    sender.send = AsyncMock()

    typing_indicator = MagicMock()
    typing_indicator.start = MagicMock(return_value=MagicMock())

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        sender=sender,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0.0),
        knowledge_gap_service=gap_service,
        conversation_state=ConversationState(),
        typing_indicator=typing_indicator,
    )

    batch = make_batch(
        "Посмотри на скриншот и помоги решить проблему.",
        base64_image="BASE64_DATA",
        mime_type="image/png",
        user_text="",
    )
    await pipeline.handle(batch)

    llm_client.chat_with_image.assert_awaited_once()
    assert llm_client.chat_with_image.await_args.args[0] == (
        "Посмотри на скриншот и помоги решить проблему."
    )
    gap_service.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_should_record_the_caption_when_the_user_wrote_one():
    llm_client = MagicMock()
    llm_client.chat_with_image = AsyncMock(return_value=LlmReply(text="Вижу ошибку подключения"))

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()

    sender = MagicMock(spec=TelegramMessageSender)
    sender.send = AsyncMock()

    typing_indicator = MagicMock()
    typing_indicator.start = MagicMock(return_value=MagicMock())

    pipeline = UserMessagePipeline(
        llm_client=llm_client,
        sender=sender,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0.0),
        knowledge_gap_service=gap_service,
        conversation_state=ConversationState(),
        typing_indicator=typing_indicator,
    )

    batch = make_batch(
        "почему n/a на всех серверах?",
        base64_image="BASE64_DATA",
        mime_type="image/png",
        user_text="почему n/a на всех серверах?",
    )
    await pipeline.handle(batch)

    gap_service.evaluate.assert_awaited_once()
    assert gap_service.evaluate.await_args.args[0] == "почему n/a на всех серверах?"


def _illustrated_context(image: str | None) -> FaqContext:
    return FaqContext(
        text="FAQ...",
        results=[FaqResult("Где кнопка?", "Слева", 0.8, 0.03, image=image)],
        max_similarity=0.8,
        best_question="Где кнопка?",
    )


def _pipeline(llm_client, sender, forwarder, gap_service) -> UserMessagePipeline:
    typing_indicator = MagicMock()
    typing_indicator.start = MagicMock(return_value=MagicMock())
    return UserMessagePipeline(
        llm_client=llm_client,
        sender=sender,
        forwarder=forwarder,
        rate_limiter=UserRateLimiter(min_interval=0.0),
        knowledge_gap_service=gap_service,
        conversation_state=ConversationState(),
        typing_indicator=typing_indicator,
    )


def _parts(image: str | None):
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        return_value=LlmReply(text="Нажмите левую кнопку", faq_context=_illustrated_context(image))
    )
    sender = MagicMock(spec=TelegramMessageSender)
    sender.send = AsyncMock()
    sender.send_photo = AsyncMock(return_value=907)
    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()
    gap_service = MagicMock()
    gap_service.evaluate = AsyncMock()
    return llm_client, sender, forwarder, gap_service


class TestIllustrations:
    """The top FAQ hit may name a screenshot to send after the answer."""

    @pytest.mark.asyncio
    async def test_sends_the_picture_named_by_the_top_hit(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        await _pipeline(llm_client, sender, forwarder, gaps).handle(make_batch("где кнопка?"))

        sender.send_photo.assert_awaited_once()
        assert sender.send_photo.await_args.args[1].name == "happ-buttons.png"

    @pytest.mark.asyncio
    async def test_the_answer_text_goes_out_before_the_picture(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        order: list[str] = []
        sender.send = AsyncMock(side_effect=lambda *a, **k: order.append("text"))
        sender.send_photo = AsyncMock(side_effect=lambda *a, **k: order.append("photo") or 907)

        await _pipeline(llm_client, sender, forwarder, gaps).handle(make_batch("где кнопка?"))

        assert order == ["text", "photo"]

    @pytest.mark.asyncio
    async def test_sends_nothing_when_the_top_hit_has_no_picture(self) -> None:
        llm_client, sender, forwarder, gaps = _parts(None)
        await _pipeline(llm_client, sender, forwarder, gaps).handle(make_batch("сколько стоит?"))

        sender.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hands_the_operator_topic_the_picture_the_user_received(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        await _pipeline(llm_client, sender, forwarder, gaps).handle(make_batch("где кнопка?"))

        assert forwarder.forward_to_support.await_args.kwargs["illustration_message_id"] == 907

    @pytest.mark.asyncio
    async def test_a_picture_that_could_not_be_sent_does_not_hold_up_the_forward(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        sender.send_photo = AsyncMock(return_value=None)

        await _pipeline(llm_client, sender, forwarder, gaps).handle(make_batch("где кнопка?"))

        forwarder.forward_to_support.assert_awaited_once()
        assert forwarder.forward_to_support.await_args.kwargs["illustration_message_id"] is None

    @pytest.mark.asyncio
    async def test_does_not_send_the_same_picture_twice_in_one_conversation(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        pipeline = _pipeline(llm_client, sender, forwarder, gaps)

        await pipeline.handle(make_batch("где кнопка обновить?"))
        await pipeline.handle(make_batch("всё равно не работает"))

        sender.send_photo.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_second_conversation_gets_the_picture_again(self) -> None:
        llm_client, sender, forwarder, gaps = _parts("happ-buttons.png")
        pipeline = _pipeline(llm_client, sender, forwarder, gaps)

        await pipeline.handle(make_batch("где кнопка обновить?", user_id=100))
        await pipeline.handle(make_batch("где кнопка обновить?", user_id=200))

        assert sender.send_photo.await_count == 2
