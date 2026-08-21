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
