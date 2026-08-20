"""Unit tests for AdminNotifier."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.admin_notifier import AdminNotifier


@pytest.mark.asyncio
async def test_notify_error_sends_to_support_group():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    notifier = AdminNotifier(bot, support_group_chat_id=-100123)
    await notifier.notify_error("MCP init failed", error=Exception("connection refused"))

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args[1]
    assert kwargs["chat_id"] == -100123
    assert kwargs["disable_notification"] is True
    assert "MCP init failed" in kwargs["text"]
    assert "connection refused" in kwargs["text"]


@pytest.mark.asyncio
async def test_notify_error_with_user_id():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    notifier = AdminNotifier(bot, support_group_chat_id=-100123)
    await notifier.notify_error("Tool call failed", user_id=4242, error=Exception("boom"))

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args[1]
    assert "User: 4242" in kwargs["text"]


@pytest.mark.asyncio
async def test_notify_error_omits_user_when_none():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    notifier = AdminNotifier(bot, support_group_chat_id=-100123)
    await notifier.notify_error("Startup failed", error=Exception("boom"))

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args[1]
    assert "User:" not in kwargs["text"]


@pytest.mark.asyncio
async def test_notify_error_truncates_overlong_error():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    notifier = AdminNotifier(bot, support_group_chat_id=-100123)
    long_msg = "X" * 10000
    await notifier.notify_error("Boom", error=Exception(long_msg))

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args[1]
    assert len(kwargs["text"]) < 4096


@pytest.mark.asyncio
async def test_notify_error_handles_telegram_exception_gracefully():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=Exception("Telegram connection closed"))

    notifier = AdminNotifier(bot, support_group_chat_id=-100123)
    # Should not throw
    await notifier.notify_error("Boom", error=Exception("original"))
