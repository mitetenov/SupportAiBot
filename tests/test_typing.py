"""Unit tests for TypingIndicator and TypingSession."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.typing import TypingIndicator, TypingSession


@pytest.mark.asyncio
async def test_typing_session_sends_chat_action_periodically():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    session = TypingSession(bot, chat_id=12345, refresh_seconds=0.05)
    session.start()

    await asyncio.sleep(0.12)
    session.close()

    # At t=0, t=0.05, t=0.10 => should be called at least 2 times
    assert bot.send_chat_action.call_count >= 2
    bot.send_chat_action.assert_called_with(chat_id=12345, action="typing")


@pytest.mark.asyncio
async def test_typing_session_async_context_manager():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    async with TypingSession(bot, chat_id=999, refresh_seconds=0.05):
        await asyncio.sleep(0.06)

    assert bot.send_chat_action.call_count >= 1
    bot.send_chat_action.assert_called_with(chat_id=999, action="typing")


@pytest.mark.asyncio
async def test_typing_indicator_start_and_shutdown():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    indicator = TypingIndicator(bot, refresh_seconds=0.05)
    session = indicator.start(chat_id=555)

    await asyncio.sleep(0.06)
    session.close()
    indicator.shutdown()

    assert bot.send_chat_action.call_count >= 1


@pytest.mark.asyncio
async def test_typing_session_handles_exceptions_gracefully():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock(side_effect=Exception("Telegram network error"))

    async with TypingSession(bot, chat_id=123, refresh_seconds=0.05):
        await asyncio.sleep(0.06)

    assert bot.send_chat_action.call_count >= 1
