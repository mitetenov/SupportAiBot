"""Unit tests for TelegramMessageSender: chunking, blank handling, error containment."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.sender import MAX_MESSAGE_LENGTH, TelegramMessageSender


def make_sender() -> tuple[TelegramMessageSender, MagicMock]:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock(return_value=MagicMock(message_id=77))
    bot.set_message_reaction = AsyncMock()
    return TelegramMessageSender(bot), bot


class TestSplit:
    def test_short_text_is_one_chunk(self) -> None:
        assert TelegramMessageSender.split("привет") == ["привет"]

    def test_text_at_the_limit_is_not_split(self) -> None:
        text = "x" * MAX_MESSAGE_LENGTH
        assert TelegramMessageSender.split(text) == [text]

    def test_every_chunk_stays_within_the_limit(self) -> None:
        text = "y" * (MAX_MESSAGE_LENGTH * 3 + 17)
        chunks = TelegramMessageSender.split(text)
        assert len(chunks) == 4
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)

    def test_splits_on_newlines_and_round_trips(self) -> None:
        source = "\n".join(f"строка {i} " + "z" * 80 for i in range(200))
        chunks = TelegramMessageSender.split(source)
        assert len(chunks) > 1
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)
        assert "\n".join(chunks) == source

    def test_a_single_unbroken_run_is_cut_at_the_limit(self) -> None:
        text = "q" * (MAX_MESSAGE_LENGTH + 1)
        assert TelegramMessageSender.split(text) == ["q" * MAX_MESSAGE_LENGTH, "q"]


class TestSend:
    @pytest.mark.asyncio
    async def test_sends_long_text_as_several_messages(self) -> None:
        sender, bot = make_sender()
        await sender.send(100, "a" * (MAX_MESSAGE_LENGTH + 500))
        assert bot.send_message.await_count == 2
        for call in bot.send_message.await_args_list:
            assert len(call.kwargs["text"]) <= MAX_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_short_send_keeps_the_plain_call_shape(self) -> None:
        sender, bot = make_sender()
        await sender.send(100, "готово")
        bot.send_message.assert_awaited_once_with(chat_id=100, text="готово")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", [None, "", "   ", "\n\t"])
    async def test_blank_text_is_never_sent(self, blank: str | None) -> None:
        sender, bot = make_sender()
        await sender.send(100, blank)
        bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_send_does_not_raise(self) -> None:
        sender, bot = make_sender()
        bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked by the user"))
        await sender.send(100, "текст")
        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_the_first_chunk_carries_the_reply_link(self) -> None:
        sender, bot = make_sender()
        await sender.send_reply(100, 55, "b" * (MAX_MESSAGE_LENGTH + 10))
        calls = bot.send_message.await_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["reply_to_message_id"] == 55
        assert "reply_to_message_id" not in calls[1].kwargs

    @pytest.mark.asyncio
    async def test_topic_send_carries_the_thread_id(self) -> None:
        sender, bot = make_sender()
        await sender.send_to_topic(-100123, 42, "в топик")
        bot.send_message.assert_awaited_once_with(
            chat_id=-100123, message_thread_id=42, text="в топик"
        )


class TestCopyAndReact:
    @pytest.mark.asyncio
    async def test_copy_returns_the_new_message_id(self) -> None:
        sender, bot = make_sender()
        assert await sender.copy_message(chat_id=1, from_chat_id=2, message_id=3) == 77

    @pytest.mark.asyncio
    async def test_copy_returns_none_when_it_fails(self) -> None:
        sender, bot = make_sender()
        bot.copy_message = AsyncMock(side_effect=RuntimeError("chat not found"))
        assert await sender.copy_message(chat_id=1, from_chat_id=2, message_id=3) is None

    @pytest.mark.asyncio
    async def test_reaction_failure_is_contained(self) -> None:
        sender, bot = make_sender()
        bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("message not found"))
        await sender.set_reaction(1, 2, [])
        bot.set_message_reaction.assert_awaited_once()
