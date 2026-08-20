"""Unit tests for UserMessageBuffer (debounce queue, max messages, concurrent batching)."""

import asyncio

import pytest

from app.bot.buffer import BufferedMessage, MessageBatch, UserMessageBuffer


class DummyChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class DummyUser:
    def __init__(self, user_id: int, username: str = "testuser"):
        self.id = user_id
        self.username = username


class DummyMessage:
    def __init__(self, message_id: int, user_id: int = 100):
        self.message_id = message_id
        self.chat = DummyChat(user_id)
        self.from_user = DummyUser(user_id)

    @property
    def user(self) -> DummyUser:
        return self.from_user


def make_msg(message_id: int, user_id: int = 100) -> DummyMessage:
    return DummyMessage(message_id, user_id)


@pytest.mark.asyncio
async def test_should_merge_messages_sent_in_quick_succession():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=100, max_messages=5)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "привет"), delivered.append)
    buffer.submit(100, BufferedMessage.from_text(make_msg(2), "не работает впн"), delivered.append)
    buffer.submit(100, BufferedMessage.from_text(make_msg(3), "что делать"), delivered.append)

    await asyncio.sleep(0.2)
    buffer.shutdown()

    assert len(delivered) == 1
    batch = delivered[0]
    assert batch.text == "привет\nне работает впн\nчто делать"
    assert batch.message_ids == [1, 2, 3]
    assert batch.size() == 3
    assert not batch.has_image()


@pytest.mark.asyncio
async def test_should_flush_immediately_when_the_batch_is_full():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=5000, max_messages=3)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "one"), delivered.append)
    buffer.submit(100, BufferedMessage.from_text(make_msg(2), "two"), delivered.append)
    buffer.submit(100, BufferedMessage.from_text(make_msg(3), "three"), delivered.append)

    # Flushed immediately because max_messages is 3
    await asyncio.sleep(0.05)
    buffer.shutdown()

    assert len(delivered) == 1
    assert delivered[0].size() == 3
    assert delivered[0].text == "one\ntwo\nthree"


@pytest.mark.asyncio
async def test_should_deliver_a_single_message_after_the_window():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=80, max_messages=5)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "один вопрос"), delivered.append)

    assert len(delivered) == 0
    await asyncio.sleep(0.15)
    buffer.shutdown()

    assert len(delivered) == 1
    assert delivered[0].text == "один вопрос"
    assert delivered[0].message_ids == [1]


@pytest.mark.asyncio
async def test_should_start_a_fresh_batch_after_a_flush():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=50, max_messages=5)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "первый"), delivered.append)
    await asyncio.sleep(0.1)
    assert len(delivered) == 1
    assert delivered[0].text == "первый"

    delivered.clear()
    buffer.submit(100, BufferedMessage.from_text(make_msg(2), "второй"), delivered.append)
    await asyncio.sleep(0.1)
    buffer.shutdown()

    assert len(delivered) == 1
    assert delivered[0].text == "второй"


@pytest.mark.asyncio
async def test_should_carry_the_image_from_a_batch():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=60, max_messages=5)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "смотри"), delivered.append)
    buffer.submit(
        100,
        BufferedMessage(
            message=make_msg(2),
            text="скриншот",
            base64_image="BASE64_DATA",
            mime_type="image/png",
        ),
        delivered.append,
    )

    await asyncio.sleep(0.15)
    buffer.shutdown()

    assert len(delivered) == 1
    batch = delivered[0]
    assert batch.has_image()
    assert batch.base64_image == "BASE64_DATA"
    assert batch.mime_type == "image/png"
    assert batch.text == "смотри\nскриншот"


@pytest.mark.asyncio
async def test_should_keep_different_users_in_separate_batches():
    delivered: list[MessageBatch] = []
    buffer = UserMessageBuffer(window_ms=80, max_messages=5)

    buffer.submit(100, BufferedMessage.from_text(make_msg(1, 100), "от первого"), delivered.append)
    buffer.submit(200, BufferedMessage.from_text(make_msg(2, 200), "от второго"), delivered.append)

    await asyncio.sleep(0.15)
    buffer.shutdown()

    assert len(delivered) == 2
    users = {b.user.id for b in delivered}
    assert users == {100, 200}


@pytest.mark.asyncio
async def test_async_sink_coroutine_execution():
    async_delivered: list[MessageBatch] = []

    async def async_sink(batch: MessageBatch):
        await asyncio.sleep(0.01)
        async_delivered.append(batch)

    buffer = UserMessageBuffer(window_ms=50, max_messages=2)
    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "async 1"), async_sink)
    buffer.submit(100, BufferedMessage.from_text(make_msg(2), "async 2"), async_sink)

    await asyncio.sleep(0.1)
    buffer.shutdown()

    assert len(async_delivered) == 1
    assert async_delivered[0].text == "async 1\nasync 2"


@pytest.mark.asyncio
async def test_async_sink_task_returning_callback_execution():
    task_delivered: list[MessageBatch] = []

    async def async_action(batch: MessageBatch):
        await asyncio.sleep(0.01)
        task_delivered.append(batch)

    def task_returning_sink(batch: MessageBatch):
        return asyncio.create_task(async_action(batch))

    buffer = UserMessageBuffer(window_ms=50, max_messages=2)
    buffer.submit(100, BufferedMessage.from_text(make_msg(1), "task 1"), task_returning_sink)
    buffer.submit(100, BufferedMessage.from_text(make_msg(2), "task 2"), task_returning_sink)

    await asyncio.sleep(0.1)
    buffer.shutdown()

    assert len(task_delivered) == 1
    assert task_delivered[0].text == "task 1\ntask 2"


@pytest.mark.asyncio
async def test_drain_answers_what_is_still_buffered():
    """A message caught inside the coalescing window must not vanish on shutdown."""
    buffer = UserMessageBuffer(window_ms=60_000, max_messages=10)
    delivered: list[MessageBatch] = []

    async def sink(batch: MessageBatch) -> None:
        delivered.append(batch)

    buffer.submit(1, BufferedMessage.from_text(make_msg(1), "не работает"), sink)
    assert buffer.pending_users() == 1

    await buffer.drain(sink)

    assert [b.text for b in delivered] == ["не работает"]
    assert buffer.pending_users() == 0


@pytest.mark.asyncio
async def test_drain_waits_for_an_answer_already_in_flight():
    buffer = UserMessageBuffer(window_ms=0, max_messages=1)
    finished = False

    async def slow_sink(_batch: MessageBatch) -> None:
        nonlocal finished
        await asyncio.sleep(0.01)
        finished = True

    buffer.submit(1, BufferedMessage.from_text(make_msg(1), "вопрос"), slow_sink)

    await buffer.drain(slow_sink)

    assert finished is True, "shutdown cut off an answer that was being written"


@pytest.mark.asyncio
async def test_drain_gives_up_on_an_answer_that_never_finishes():
    buffer = UserMessageBuffer(window_ms=0, max_messages=1)

    async def hanging_sink(_batch: MessageBatch) -> None:
        await asyncio.sleep(3600)

    buffer.submit(1, BufferedMessage.from_text(make_msg(1), "вопрос"), hanging_sink)

    await buffer.drain(hanging_sink, timeout=0.01)

    assert buffer._inflight == set()


@pytest.mark.asyncio
async def test_drain_on_an_idle_buffer_does_nothing():
    buffer = UserMessageBuffer(window_ms=1000, max_messages=5)
    calls: list[MessageBatch] = []

    async def sink(batch: MessageBatch) -> None:
        calls.append(batch)

    await buffer.drain(sink)

    assert calls == []
