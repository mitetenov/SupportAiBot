"""Unit tests for the ticket answering bookkeeping."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.state import TicketStateStore
from app.storage.models import BedolagaTicketState


class _FakeDbManager:
    """A session manager whose session is one mock everybody can inspect."""

    def __init__(self, row: Any = None) -> None:
        self.session_obj = MagicMock()
        self.session_obj.get = AsyncMock(return_value=row)
        self.session_obj.merge = AsyncMock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self.session_obj


class TestAlreadyAnswered:
    """The same message must never be answered twice."""

    async def test_false_when_the_ticket_is_unknown(self) -> None:
        db = _FakeDbManager(row=None)
        assert await TicketStateStore(db).already_answered(17, 100) is False

    async def test_true_when_that_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        assert await TicketStateStore(db).already_answered(17, 100) is True

    async def test_true_when_a_later_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=120))
        assert await TicketStateStore(db).already_answered(17, 100) is True

    async def test_false_for_a_message_newer_than_the_last_answer(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        assert await TicketStateStore(db).already_answered(17, 101) is False


class TestMarkAnswered:
    """Recording an answer upserts a single row per ticket."""

    async def test_merges_the_row(self) -> None:
        db = _FakeDbManager()
        await TicketStateStore(db).mark_answered(17, 101)
        merged = db.session_obj.merge.await_args.args[0]
        assert isinstance(merged, BedolagaTicketState)
        assert merged.ticket_id == 17
        assert merged.last_answered_message_id == 101
