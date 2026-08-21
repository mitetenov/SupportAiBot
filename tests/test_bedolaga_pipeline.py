"""Unit tests for answering a Bedolaga ticket."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.state import TicketProgress
from app.bedolaga.types import TELEGRAM_ID_UNKNOWN, TelegramIdLookup, Ticket, TicketMessage
from app.bot.conversation_state import ConversationState
from app.bot.rate_limiter import UserRateLimiter
from app.constants import get_message
from app.llm.base import LlmReply

TICKET_ID = 17
PANEL_USER_ID = 55
TELEGRAM_ID = 42
#: The id the panel gives the bot's own reply when it accepts one.
BOT_REPLY_ID = 500


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
    lookup: TelegramIdLookup | None = None,
    last_bot_reply_message_id: int = 0,
    max_concurrent: int = 5,
    rate_limiter: UserRateLimiter | None = None,
) -> tuple[TicketAnswerer, dict[str, Any]]:
    client = MagicMock()
    client.get_ticket = AsyncMock(return_value=ticket if ticket is not None else _ticket())
    client.resolve_telegram_id = AsyncMock(
        return_value=lookup
        if lookup is not None
        else TelegramIdLookup(known=True, telegram_id=telegram_id)
    )
    client.reply = AsyncMock(return_value=BOT_REPLY_ID if reply_ok else None)
    client.set_priority = AsyncMock(return_value=True)

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        return_value=reply if reply is not None else LlmReply(text="Проверьте подписку")
    )

    state = MagicMock()
    state.progress = AsyncMock(
        return_value=TicketProgress(
            last_answered_message_id=10**9 if already_answered else 0,
            last_bot_reply_message_id=last_bot_reply_message_id,
        )
    )
    state.record_reply = AsyncMock()

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
        rate_limiter=rate_limiter or UserRateLimiter(),
        admin_notifier=admin_notifier,
        forwarder=forwarder,
        knowledge_gap_service=knowledge_gap_service,
        conversation_state=conversation_state or ConversationState(),
        max_concurrent=max_concurrent,
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
        parts["state"].record_reply.assert_awaited_once_with(
            TICKET_ID, BOT_REPLY_ID, answered_message_id=100
        )

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
        parts["state"].record_reply.assert_not_awaited()
        parts["admin_notifier"].notify_error.assert_awaited()

    async def test_a_rate_limited_user_is_left_for_the_next_sweep(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.reset_mock()
        parts["state"].record_reply.reset_mock()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()
        parts["state"].record_reply.assert_not_awaited()


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
        parts["state"].record_reply.assert_not_awaited()


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
        parts["state"].record_reply.assert_awaited_once_with(
            TICKET_ID, BOT_REPLY_ID, answered_message_id=100
        )


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
        parts["state"].record_reply.assert_not_awaited()

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


class TestNothingToAnswer:
    """A screenshot with no words, on a model that cannot see it.

    The commonest ticket in the tracker is "пришлите скриншот" followed by a
    bare picture, and the default provider is text-only. Asking the model
    anyway means asking it nothing, and `client.reply` publishes whatever it
    invents to the user as support's answer — permanently, because the message
    is then marked answered and never revisited.
    """

    def _screenshot_thread(self) -> Ticket:
        return _ticket(
            TicketMessage(id=100, text="Не работает", is_from_admin=False),
            TicketMessage(id=101, text="Пришлите скриншот", is_from_admin=True),
            TicketMessage(id=102, text="", is_from_admin=False, has_media=True, media_type="photo"),
            title="",
        )

    def _blind(self, answerer: TicketAnswerer, parts: dict[str, Any]) -> None:
        parts["client"].download_media = AsyncMock()
        parts["llm_client"].supports_images = MagicMock(return_value=False)
        parts["llm_client"].chat_with_image = AsyncMock()
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

    async def test_never_asks_the_model_an_empty_question(self) -> None:
        answerer, parts = _answerer(ticket=self._screenshot_thread())
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat.assert_not_awaited()
        parts["llm_client"].chat_with_image.assert_not_awaited()

    async def test_does_not_burn_the_message(self) -> None:
        """Unmarked on purpose: the user's next message is still ours to answer.

        The hand-over line is still the bot's own message and is recorded as
        such — otherwise the sweep after it reads it as an operator stepping
        into the ticket and the bot never speaks again.
        """
        answerer, parts = _answerer(ticket=self._screenshot_thread())
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        parts["state"].record_reply.assert_awaited_once_with(TICKET_ID, BOT_REPLY_ID)
        assert "answered_message_id" not in parts["state"].record_reply.await_args.kwargs

    async def test_asks_the_user_for_words_in_the_ticket(self) -> None:
        answerer, parts = _answerer(ticket=self._screenshot_thread())
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        parts["client"].reply.assert_awaited_once_with(
            TICKET_ID, get_message("bedolaga.nothing.to.answer")
        )

    async def test_calls_a_human_into_the_topic(self) -> None:
        answerer, parts = _answerer(ticket=self._screenshot_thread())
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        kwargs = parts["forwarder"].forward_to_support.await_args.kwargs
        assert kwargs["needs_escalation"] is True
        assert str(TICKET_ID) in kwargs["bot_response"]

    async def test_records_no_knowledge_gap_for_a_question_nobody_asked(self) -> None:
        answerer, parts = _answerer(ticket=self._screenshot_thread())
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        parts["knowledge_gap_service"].evaluate.assert_not_awaited()

    async def test_reports_a_panel_that_rejects_the_handover(self) -> None:
        answerer, parts = _answerer(ticket=self._screenshot_thread(), reply_ok=False)
        self._blind(answerer, parts)

        await answerer.handle(TICKET_ID)

        parts["admin_notifier"].notify_error.assert_awaited()

    async def test_a_vision_model_still_answers_the_same_ticket(self) -> None:
        """The guard must not cost the working path anything."""
        from app.bedolaga.types import ImageAttachment

        answerer, parts = _answerer(ticket=self._screenshot_thread())
        parts["client"].download_media = AsyncMock(
            return_value=ImageAttachment(base64_image="Zm9v", mime_type="image/png")
        )
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock(return_value=LlmReply(text="Видно ошибку"))
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["client"].reply.assert_awaited_once_with(TICKET_ID, "Видно ошибку")
        parts["state"].record_reply.assert_awaited_once_with(
            TICKET_ID, BOT_REPLY_ID, answered_message_id=102
        )

    async def test_an_attachment_the_bot_does_not_read_hands_over_too(self) -> None:
        """A voice note is media, but never one the vision path can use."""
        ticket = _ticket(
            TicketMessage(id=100, text="Не работает", is_from_admin=False),
            TicketMessage(id=101, text="Что именно?", is_from_admin=True),
            TicketMessage(id=102, text="", is_from_admin=False, has_media=True, media_type="voice"),
            title="",
        )
        answerer, parts = _answerer(ticket=ticket)
        parts["client"].download_media = AsyncMock()
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["client"].download_media.assert_not_awaited()
        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_awaited_once_with(
            TICKET_ID, get_message("bedolaga.nothing.to.answer")
        )


class TestUnresolvedIdentity:
    """A panel blip must not mint a permanent identity for a Telegram user."""

    async def test_does_not_answer_when_the_panel_does_not_say_who_this_is(self) -> None:
        answerer, parts = _answerer(lookup=TELEGRAM_ID_UNKNOWN)
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_not_awaited()

    async def test_leaves_the_message_for_the_next_sweep(self) -> None:
        answerer, parts = _answerer(lookup=TELEGRAM_ID_UNKNOWN)
        await answerer.handle(TICKET_ID)
        parts["state"].record_reply.assert_not_awaited()

    async def test_does_not_open_a_cabinet_topic_for_a_telegram_user(self) -> None:
        """The forum topic and its TopicMapping row would outlive the outage."""
        answerer, parts = _answerer(lookup=TELEGRAM_ID_UNKNOWN)
        await answerer.handle(TICKET_ID)
        parts["forwarder"].forward_to_support.assert_not_awaited()

    async def test_does_not_wake_the_admins_over_a_transient_blip(self) -> None:
        answerer, parts = _answerer(lookup=TELEGRAM_ID_UNKNOWN)
        await answerer.handle(TICKET_ID)
        parts["admin_notifier"].notify_error.assert_not_awaited()

    async def test_a_definite_no_telegram_id_still_answers_under_the_negative_key(self) -> None:
        answerer, parts = _answerer(lookup=TelegramIdLookup(known=True, telegram_id=None))
        await answerer.handle(TICKET_ID)
        assert parts["llm_client"].chat.await_args.args[1] == -PANEL_USER_ID
        parts["client"].reply.assert_awaited_once()

    async def test_a_resolved_lookup_still_answers_under_the_telegram_id(self) -> None:
        answerer, parts = _answerer(lookup=TelegramIdLookup(known=True, telegram_id=TELEGRAM_ID))
        await answerer.handle(TICKET_ID)
        assert parts["llm_client"].chat.await_args.args[1] == TELEGRAM_ID
        parts["client"].reply.assert_awaited_once()


class TestReplyBackoff:
    """A reply that cannot land must not be bought again every minute.

    A key without write scope, or a `/reply` endpoint answering 500, leaves the
    ticket open. The next sweep re-reads it, pays for a whole model turn and
    hits the same wall — for every affected ticket, forever, plus one admin
    alert each time round.
    """

    def _failing(self) -> tuple[TicketAnswerer, dict[str, Any]]:
        # No rate-limit interval: the backoff has to be what stops the second
        # turn, otherwise this test passes without it.
        return _answerer(reply_ok=False, rate_limiter=UserRateLimiter(min_interval=0.0))

    async def test_the_next_sweep_does_not_pay_for_another_model_call(self) -> None:
        answerer, parts = self._failing()

        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.reset_mock()
        parts["client"].reply.reset_mock()
        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_not_awaited()

    async def test_the_next_sweep_does_not_wake_the_admins_again(self) -> None:
        answerer, parts = self._failing()

        await answerer.handle(TICKET_ID)
        await answerer.handle(TICKET_ID)

        assert parts["admin_notifier"].notify_error.await_count == 1

    async def test_the_ticket_is_tried_again_once_the_window_passes(self) -> None:
        answerer, parts = self._failing()

        await answerer.handle(TICKET_ID)
        for entry in answerer._reply_backoff.values():
            entry.retry_at = 0.0
        parts["llm_client"].chat.reset_mock()
        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat.assert_awaited_once()

    async def test_the_wait_grows_with_every_failure(self) -> None:
        answerer, _ = self._failing()

        await answerer.handle(TICKET_ID)
        first = answerer._reply_backoff[TICKET_ID].retry_at
        answerer._reply_backoff[TICKET_ID].retry_at = 0.0
        await answerer.handle(TICKET_ID)
        second = answerer._reply_backoff[TICKET_ID].retry_at

        assert answerer._reply_backoff[TICKET_ID].failures == 2
        assert second > first

    async def test_a_reply_that_lands_clears_the_backoff(self) -> None:
        answerer, parts = self._failing()

        await answerer.handle(TICKET_ID)
        answerer._reply_backoff[TICKET_ID].retry_at = 0.0
        parts["client"].reply = AsyncMock(return_value=BOT_REPLY_ID)
        answerer.client = parts["client"]
        await answerer.handle(TICKET_ID)

        assert TICKET_ID not in answerer._reply_backoff

    async def test_a_hand_over_that_cannot_land_backs_off_too(self) -> None:
        """No model call to waste here, but the same endless loop otherwise."""
        ticket = _ticket(
            TicketMessage(id=100, text="Не работает", is_from_admin=False),
            TicketMessage(id=101, text="Пришлите скриншот", is_from_admin=True),
            TicketMessage(id=102, text="", is_from_admin=False, has_media=True, media_type="voice"),
            title="",
        )
        answerer, parts = _answerer(
            ticket=ticket,
            reply_ok=False,
            last_bot_reply_message_id=101,
            rate_limiter=UserRateLimiter(min_interval=0.0),
        )
        parts["llm_client"].supports_images = MagicMock(return_value=False)
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)
        parts["client"].reply.reset_mock()
        await answerer.handle(TICKET_ID)

        parts["client"].reply.assert_not_awaited()

    async def test_a_ticket_that_stops_awaiting_an_answer_is_forgotten(self) -> None:
        """Otherwise every ticket that ever failed stays in the map for good."""
        answerer, parts = self._failing()

        await answerer.handle(TICKET_ID)
        assert TICKET_ID in answerer._reply_backoff
        parts["client"].get_ticket = AsyncMock(return_value=_ticket(status="closed"))
        answerer.client = parts["client"]
        await answerer.handle(TICKET_ID)

        assert TICKET_ID not in answerer._reply_backoff


class TestConcurrencyCap:
    """One sweep can bring back a hundred tickets, each holding a connection.

    The pool is shared with the LLM and embedding providers, so an uncapped
    backlog starves plain Telegram messages of connections — a feature that has
    nothing to do with Bedolaga degrades because Bedolaga was switched on.
    """

    def _two_tickets(self, max_concurrent: int) -> tuple[TicketAnswerer, dict[str, Any], list[int]]:
        async def get_ticket(ticket_id: int) -> Ticket:
            await asyncio.sleep(0)
            return Ticket(
                id=ticket_id,
                user_id=PANEL_USER_ID + ticket_id,
                title="Не подключается",
                status="open",
                messages=(TicketMessage(id=100, text="Помогите", is_from_admin=False),),
            )

        in_flight = 0
        peaks: list[int] = []

        async def chat(_question: str, _user_key: int) -> LlmReply:
            nonlocal in_flight
            in_flight += 1
            peaks.append(in_flight)
            # Several suspension points, the way a real model call has: without
            # them the first turn runs to completion before the second starts
            # and the cap is never what kept them apart.
            for _ in range(4):
                await asyncio.sleep(0)
            in_flight -= 1
            return LlmReply(text="Проверьте подписку")

        answerer, parts = _answerer(
            max_concurrent=max_concurrent,
            # The per-user rate limiter must not be what serialises these.
            rate_limiter=UserRateLimiter(min_interval=0.0),
        )
        parts["client"].get_ticket = AsyncMock(side_effect=get_ticket)
        parts["llm_client"].chat = AsyncMock(side_effect=chat)
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]
        return answerer, parts, peaks

    async def test_two_tickets_do_not_reach_the_model_at_once(self) -> None:
        answerer, parts, peaks = self._two_tickets(max_concurrent=1)

        await asyncio.gather(answerer.handle(17), answerer.handle(18))

        assert parts["llm_client"].chat.await_count == 2
        assert max(peaks) == 1

    async def test_a_wider_cap_really_does_let_them_overlap(self) -> None:
        """The control: without this, the test above would pass with no cap."""
        answerer, _, peaks = self._two_tickets(max_concurrent=2)

        await asyncio.gather(answerer.handle(17), answerer.handle(18))

        assert max(peaks) == 2


class TestOperatorInThePanel:
    """`KeyedLock` and the Telegram flag do not cover the admin UI.

    An operator answering inside Bedolaga's own panel sets nothing this bot can
    see. The only evidence is an admin message in the ticket that the bot did
    not write — and every reply in a ticket is an admin message, its own
    included, which is why it records the id of its own.
    """

    def _thread_with_a_human(self) -> Ticket:
        return _ticket(
            TicketMessage(id=100, text="Помогите", is_from_admin=False),
            TicketMessage(id=101, text="Проверьте подписку", is_from_admin=True),
            TicketMessage(id=105, text="Здравствуйте, сейчас посмотрю", is_from_admin=True),
            TicketMessage(id=106, text="Спасибо", is_from_admin=False),
        )

    async def test_does_not_answer_over_a_human(self) -> None:
        answerer, parts = _answerer(
            ticket=self._thread_with_a_human(), last_bot_reply_message_id=101
        )
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_not_awaited()

    async def test_does_not_mark_the_message_answered(self) -> None:
        answerer, parts = _answerer(
            ticket=self._thread_with_a_human(), last_bot_reply_message_id=101
        )
        await answerer.handle(TICKET_ID)
        parts["state"].record_reply.assert_not_awaited()

    async def test_puts_the_question_in_the_topic_with_an_escalation(self) -> None:
        answerer, parts = _answerer(
            ticket=self._thread_with_a_human(), last_bot_reply_message_id=101
        )
        await answerer.handle(TICKET_ID)
        kwargs = parts["forwarder"].forward_to_support.await_args.kwargs
        assert kwargs["needs_escalation"] is True
        assert "Спасибо" in kwargs["bot_response"]

    async def test_the_notice_goes_out_once_per_message(self) -> None:
        """Shares the dedup with the Telegram-side trigger: same meaning."""
        answerer, parts = _answerer(
            ticket=self._thread_with_a_human(), last_bot_reply_message_id=101
        )
        await answerer.handle(TICKET_ID)
        await answerer.handle(TICKET_ID)
        assert parts["forwarder"].forward_to_support.await_count == 1

    async def test_the_bots_own_reply_is_not_mistaken_for_a_human(self) -> None:
        """The common path, and the one a false positive would silence forever."""
        ticket = _ticket(
            TicketMessage(id=100, text="Помогите", is_from_admin=False),
            TicketMessage(id=101, text="Проверьте подписку", is_from_admin=True),
            TicketMessage(id=102, text="Всё ещё не работает", is_from_admin=False),
        )
        answerer, parts = _answerer(ticket=ticket, last_bot_reply_message_id=101)

        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat.assert_awaited_once()
        parts["state"].record_reply.assert_awaited_once_with(
            TICKET_ID, BOT_REPLY_ID, answered_message_id=102
        )

    async def test_a_ticket_the_bot_never_replied_on_is_still_answered(self) -> None:
        """Nothing recorded means nothing to compare against, not "a human"."""
        ticket = _ticket(
            TicketMessage(id=100, text="Помогите", is_from_admin=False),
            TicketMessage(id=101, text="Проверьте подписку", is_from_admin=True),
            TicketMessage(id=102, text="Всё ещё не работает", is_from_admin=False),
        )
        answerer, parts = _answerer(ticket=ticket, last_bot_reply_message_id=0)

        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat.assert_awaited_once()
        parts["client"].reply.assert_awaited_once()
