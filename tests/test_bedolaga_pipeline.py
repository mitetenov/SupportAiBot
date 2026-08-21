"""Unit tests for answering a Bedolaga ticket."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.types import Ticket, TicketMessage
from app.bot.conversation_state import ConversationState
from app.bot.rate_limiter import UserRateLimiter
from app.llm.base import LlmReply

TICKET_ID = 17
PANEL_USER_ID = 55
TELEGRAM_ID = 42


def _ticket(
    *messages: TicketMessage,
    status: str = "open",
    title: str = "Не подключается",
) -> Ticket:
    return Ticket(
        id=TICKET_ID,
        user_id=PANEL_USER_ID,
        title=title,
        status=status,
        messages=messages or (TicketMessage(id=100, text="Помогите", is_from_admin=False),),
    )


def _answerer(
    ticket: Ticket | None = None,
    reply: LlmReply | None = None,
    already_answered: bool = False,
    telegram_id: int | None = TELEGRAM_ID,
    reply_ok: bool = True,
    conversation_state: ConversationState | None = None,
) -> tuple[TicketAnswerer, dict[str, Any]]:
    client = MagicMock()
    client.get_ticket = AsyncMock(return_value=ticket if ticket is not None else _ticket())
    client.resolve_telegram_id = AsyncMock(return_value=telegram_id)
    client.reply = AsyncMock(return_value=reply_ok)
    client.set_priority = AsyncMock(return_value=True)

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        return_value=reply if reply is not None else LlmReply(text="Проверьте подписку")
    )

    state = MagicMock()
    state.already_answered = AsyncMock(return_value=already_answered)
    state.mark_answered = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    admin_notifier = MagicMock()
    admin_notifier.notify_error = AsyncMock()

    knowledge_gap_service = MagicMock()
    knowledge_gap_service.evaluate = AsyncMock()

    answerer = TicketAnswerer(
        client=client,
        llm_client=llm_client,
        state=state,
        rate_limiter=UserRateLimiter(),
        admin_notifier=admin_notifier,
        forwarder=forwarder,
        knowledge_gap_service=knowledge_gap_service,
        conversation_state=conversation_state or ConversationState(),
    )
    parts = {
        "client": client,
        "llm_client": llm_client,
        "state": state,
        "forwarder": forwarder,
        "admin_notifier": admin_notifier,
        "knowledge_gap_service": knowledge_gap_service,
    }
    return answerer, parts


class TestAnswering:
    """The happy path: read the ticket, ask the model, write the answer back."""

    async def test_asks_the_model_under_the_telegram_id_of_the_author(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        question, user_id = parts["llm_client"].chat.await_args.args
        assert question == "Не подключается\n\nПомогите"
        assert user_id == TELEGRAM_ID

    async def test_posts_the_answer_into_the_ticket(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_awaited_once_with(TICKET_ID, "Проверьте подписку")

    async def test_records_the_answered_message(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_awaited_once_with(TICKET_ID, 100)

    async def test_strips_the_escalation_marker_from_the_answer(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Держите [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        assert parts["client"].reply.await_args.args[1].startswith("Держите")
        assert "[ESCALATE]" not in parts["client"].reply.await_args.args[1]

    async def test_falls_back_to_a_handover_line_when_the_model_says_nothing(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="   "))
        await answerer.handle(TICKET_ID)
        assert "оператору" in parts["client"].reply.await_args.args[1]


class TestSkipping:
    """Everything that must not produce a reply."""

    async def test_ignores_a_ticket_that_cannot_be_read(self) -> None:
        answerer, parts = _answerer()
        parts["client"].get_ticket = AsyncMock(return_value=None)
        answerer.client = parts["client"]
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_ticket_whose_last_message_is_ours(self) -> None:
        ticket = _ticket(
            TicketMessage(id=100, text="Помогите", is_from_admin=False),
            TicketMessage(id=101, text="Проверьте подписку", is_from_admin=True),
            status="answered",
        )
        answerer, parts = _answerer(ticket=ticket)
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_closed_ticket(self) -> None:
        answerer, parts = _answerer(ticket=_ticket(status="closed"))
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_message_already_answered(self) -> None:
        answerer, parts = _answerer(already_answered=True)
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()

    async def test_does_not_record_an_answer_the_panel_rejected(self) -> None:
        answerer, parts = _answerer(reply_ok=False)
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_not_awaited()
        parts["admin_notifier"].notify_error.assert_awaited()

    async def test_a_rate_limited_user_is_left_for_the_next_sweep(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.reset_mock()
        parts["state"].mark_answered.reset_mock()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()
        parts["state"].mark_answered.assert_not_awaited()


class TestCabinetOnlyUsers:
    """A cabinet account without a Telegram id still gets an answer."""

    async def test_uses_a_synthetic_negative_key(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        _, user_id = parts["llm_client"].chat.await_args.args
        assert user_id == -PANEL_USER_ID

    async def test_still_answers_the_ticket(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_awaited_once()


class TestFailureHandling:
    """A model failure is reported, never silently swallowed."""

    async def test_notifies_admins_when_the_turn_raises(self) -> None:
        answerer, parts = _answerer()
        parts["llm_client"].chat = AsyncMock(side_effect=RuntimeError("boom"))
        answerer.llm_client = parts["llm_client"]
        await answerer.handle(TICKET_ID)
        parts["admin_notifier"].notify_error.assert_awaited()
        parts["state"].mark_answered.assert_not_awaited()


class TestScheduling:
    """Webhook delivery must return at once, so the work runs in the background."""

    async def test_schedule_runs_the_turn_and_drain_waits_for_it(self) -> None:
        answerer, parts = _answerer()
        answerer.schedule(TICKET_ID)
        await answerer.drain()
        parts["client"].reply.assert_awaited_once()

    async def test_drain_is_a_no_op_without_work(self) -> None:
        answerer, _ = _answerer()
        await answerer.drain()


class TestEscalation:
    """A ticket the model cannot close is handed to a human, loudly."""

    async def test_appends_a_handover_line_when_the_model_asks(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        sent = parts["client"].reply.await_args.args[1]
        assert sent.startswith("Не могу помочь")
        assert "оператор" in sent.lower()

    async def test_raises_the_ticket_priority(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_awaited_once_with(TICKET_ID, "high")

    async def test_escalates_when_the_user_asks_for_a_human(self) -> None:
        ticket = _ticket(
            TicketMessage(id=100, text="Хочу поговорить с оператором", is_from_admin=False)
        )
        answerer, parts = _answerer(ticket=ticket)
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_awaited_once_with(TICKET_ID, "high")

    async def test_tags_the_mirrored_message_for_escalation(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        assert parts["forwarder"].forward_to_support.await_args.kwargs["needs_escalation"] is True

    async def test_leaves_priority_alone_on_an_ordinary_answer(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_not_awaited()


class TestMirroring:
    """Operators read the support group, so the ticket turn shows up there too."""

    async def test_mirrors_question_and_answer_into_the_topic(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        kwargs = parts["forwarder"].forward_to_support.await_args.kwargs
        assert kwargs["user_chat_id"] == TELEGRAM_ID
        assert kwargs["user_message_ids"] is None
        assert "Помогите" in kwargs["bot_response"]
        assert "Проверьте подписку" in kwargs["bot_response"]
        assert str(TICKET_ID) in kwargs["bot_response"]

    async def test_names_a_cabinet_only_user_in_the_topic_title(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        user = parts["forwarder"].forward_to_support.await_args.kwargs["user"]
        assert user.id == -PANEL_USER_ID
        assert str(PANEL_USER_ID) in (user.first_name or "")

    async def test_a_failing_mirror_does_not_lose_the_answer(self) -> None:
        answerer, parts = _answerer()
        parts["forwarder"].forward_to_support = AsyncMock(side_effect=RuntimeError("no topic"))
        answerer.forwarder = parts["forwarder"]
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_awaited_once_with(TICKET_ID, 100)


class TestOperatorSuppression:
    """While a human is holding the conversation, the bot stays out of it."""

    async def test_does_not_answer_the_ticket(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()
        parts["llm_client"].chat.assert_not_awaited()

    async def test_puts_the_question_in_the_topic_instead(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        text = parts["forwarder"].forward_to_support.await_args.kwargs["bot_response"]
        assert "Помогите" in text

    async def test_does_not_mark_the_message_answered(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_not_awaited()

    async def test_mirrors_the_notice_once_per_message(self) -> None:
        """The ticket stays open, so every sweep reads the same message again.

        Without a guard the operator's topic collects one identical notice per
        sweep for the whole suppression window.
        """
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)

        await answerer.handle(TICKET_ID)
        await answerer.handle(TICKET_ID)

        assert parts["forwarder"].forward_to_support.await_count == 1

    async def test_mirrors_again_when_the_user_writes_another_message(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)

        await answerer.handle(TICKET_ID)
        parts["client"].get_ticket = AsyncMock(
            return_value=_ticket(
                TicketMessage(id=100, text="Помогите", is_from_admin=False),
                TicketMessage(id=101, text="Алло?", is_from_admin=False),
            )
        )
        await answerer.handle(TICKET_ID)

        assert parts["forwarder"].forward_to_support.await_count == 2


class TestKnowledgeGaps:
    """A ticket nobody could answer is a gap in the FAQ, same as a chat message."""

    async def test_evaluates_the_question(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        query, user_id, raw_response, _ = parts["knowledge_gap_service"].evaluate.await_args.args
        assert "Помогите" in query
        assert user_id == TELEGRAM_ID
        assert raw_response == "Проверьте подписку"


class TestScreenshots:
    """A ticket is often a screenshot with two words under it."""

    def _photo_ticket(self) -> Ticket:
        return _ticket(
            TicketMessage(
                id=100,
                text="",
                is_from_admin=False,
                has_media=True,
                media_type="photo",
            )
        )

    async def test_sends_the_screenshot_to_the_model(self) -> None:
        from app.bedolaga.types import ImageAttachment

        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(
            return_value=ImageAttachment(base64_image="Zm9v", mime_type="image/png")
        )
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock(return_value=LlmReply(text="Видно ошибку"))
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        args = parts["llm_client"].chat_with_image.await_args.args
        assert args[1] == TELEGRAM_ID
        assert args[2] == "Zm9v"
        assert args[3] == "image/png"
        parts["client"].reply.assert_awaited_once_with(TICKET_ID, "Видно ошибку")

    async def test_asks_about_the_picture_when_there_is_no_text(self) -> None:
        from app.bedolaga.types import ImageAttachment

        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(
            return_value=ImageAttachment(base64_image="Zm9v", mime_type="image/png")
        )
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock(return_value=LlmReply(text="Видно ошибку"))
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        assert parts["llm_client"].chat_with_image.await_args.args[0].strip() != ""

    async def test_falls_back_to_text_when_the_download_fails(self) -> None:
        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(return_value=None)
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock()
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat_with_image.assert_not_awaited()
        parts["llm_client"].chat.assert_awaited_once()

    async def test_ignores_media_a_text_only_model_cannot_read(self) -> None:
        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock()
        parts["llm_client"].supports_images = MagicMock(return_value=False)
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["client"].download_media.assert_not_awaited()
        parts["llm_client"].chat.assert_awaited_once()
