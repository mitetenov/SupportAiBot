"""Operator replies from support topics are recorded as human ticket activity."""

from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.client import PostedTicketReply
from app.bedolaga.relay import TicketOperatorRelay
from app.bot.conversation_state import ConversationState

DEFAULT_POSTED_REPLY = PostedTicketReply(message_id=701)


def _relay(posted: PostedTicketReply | None = DEFAULT_POSTED_REPLY):
    client = MagicMock()
    client.reply = AsyncMock(return_value=posted)
    client.reply_with_photo = AsyncMock(return_value=posted)
    state = MagicMock()
    state.record_human_reply = AsyncMock()
    conversation = ConversationState()
    relay = TicketOperatorRelay(client, state, conversation)
    return relay, client, state, conversation


async def test_text_reply_lands_in_ticket_and_marks_human_activity() -> None:
    relay, client, state, conversation = _relay()

    assert await relay.reply_text(17, -55, "Проверяю оплату") is True

    client.reply.assert_awaited_once_with(17, "Проверяю оплату")
    state.record_human_reply.assert_awaited_once_with(17, 701)
    assert conversation.is_operator_recently_active(-55)


async def test_photo_reply_uses_bedolaga_upload_path() -> None:
    relay, client, state, conversation = _relay()

    assert await relay.reply_photo(17, 42, "Zm9v", "image/png", "Скриншот") is True

    client.reply_with_photo.assert_awaited_once_with(
        17,
        "Скриншот",
        "Zm9v",
        "image/png",
    )
    state.record_human_reply.assert_awaited_once_with(17, 701)
    assert conversation.is_operator_recently_active(42)


async def test_failed_delivery_is_not_recorded_or_confirmed() -> None:
    relay, _client, state, conversation = _relay(posted=None)

    assert await relay.reply_text(17, 42, "Ответ") is False

    state.record_human_reply.assert_not_awaited()
    assert not conversation.is_operator_recently_active(42)


async def test_unexpected_client_failure_is_contained() -> None:
    relay, client, state, conversation = _relay()
    client.reply = AsyncMock(side_effect=RuntimeError("bad response"))

    assert await relay.reply_text(17, 42, "Ответ") is False

    state.record_human_reply.assert_not_awaited()
    assert not conversation.is_operator_recently_active(42)
