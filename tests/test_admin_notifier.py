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


class TestThrottling:
    """A failure that repeats every sweep must not bury the support group.

    A revoked write scope or a `/reply` endpoint answering 500 produces one
    alert per ticket per sweep, forever. Telegram drops most of them anyway and
    `send_message` swallows the error, so the group loses the signal twice over.
    """

    async def test_repeats_within_the_window_are_sent_once(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()

        notifier = AdminNotifier(bot, support_group_chat_id=-100123)
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))

        assert bot.send_message.await_count == 1

    async def test_the_next_alert_says_how_many_it_stands_for(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()

        notifier = AdminNotifier(bot, support_group_chat_id=-100123)
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))

        # The window passes; the failure is still happening.
        for entry in notifier._throttled.values():
            entry.sent_at -= AdminNotifier.THROTTLE_WINDOW_SECONDS + 1
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))

        assert bot.send_message.await_count == 2
        assert "2" in bot.send_message.await_args.kwargs["text"]

    async def test_a_different_context_is_never_held_back(self) -> None:
        """Throttling is per-context: an unrelated failure is unrelated news."""
        bot = MagicMock()
        bot.send_message = AsyncMock()

        notifier = AdminNotifier(bot, support_group_chat_id=-100123)
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))
        await notifier.notify_error("MCP init failed", error=Exception("boom"))

        assert bot.send_message.await_count == 2

    async def test_a_first_alert_carries_no_suppression_note(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()

        notifier = AdminNotifier(bot, support_group_chat_id=-100123)
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))

        assert "подавлено" not in bot.send_message.await_args.kwargs["text"]

    async def test_stale_contexts_do_not_accumulate_forever(self) -> None:
        """Ticket ids go into the context string, so the map has to be swept."""
        bot = MagicMock()
        bot.send_message = AsyncMock()

        notifier = AdminNotifier(bot, support_group_chat_id=-100123)
        await notifier.notify_error("Ticket 17 failed", error=Exception("boom"))
        for entry in notifier._throttled.values():
            entry.sent_at -= 3 * AdminNotifier.THROTTLE_WINDOW_SECONDS

        await notifier.notify_error("Ticket 999 failed", error=Exception("boom"))

        assert list(notifier._throttled) == ["Ticket 999 failed"]
