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
        progress = await TicketStateStore(db).progress(17)
        assert progress.already_answered(100) is False

    async def test_true_when_that_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        progress = await TicketStateStore(db).progress(17)
        assert progress.already_answered(100) is True

    async def test_true_when_a_later_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=120))
        progress = await TicketStateStore(db).progress(17)
        assert progress.already_answered(100) is True

    async def test_false_for_a_message_newer_than_the_last_answer(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        progress = await TicketStateStore(db).progress(17)
        assert progress.already_answered(101) is False


class TestTheBotsOwnReply:
    """Every reply in a ticket is an admin message — including a human's."""

    async def test_a_ticket_the_bot_never_replied_on_accuses_nobody(self) -> None:
        """No reply of its own means nothing to compare an admin message to."""
        db = _FakeDbManager(row=None)
        progress = await TicketStateStore(db).progress(17)
        assert progress.last_bot_reply_message_id == 0
        assert progress.someone_else_wrote(105) is False

    async def test_an_admin_message_newer_than_the_bots_own_is_a_humans(self) -> None:
        db = _FakeDbManager(
            row=BedolagaTicketState(
                ticket_id=17,
                last_answered_message_id=100,
                last_bot_reply_message_id=101,
            )
        )
        progress = await TicketStateStore(db).progress(17)
        assert progress.someone_else_wrote(105) is True
        assert progress.someone_else_wrote(101) is False
        assert progress.someone_else_wrote(99) is False


class TestRecordReply:
    """Recording an answer upserts a single row per ticket."""

    async def test_merges_both_ids(self) -> None:
        db = _FakeDbManager()
        await TicketStateStore(db).record_reply(17, 101, answered_message_id=100)
        merged = db.session_obj.merge.await_args.args[0]
        assert isinstance(merged, BedolagaTicketState)
        assert merged.ticket_id == 17
        assert merged.last_answered_message_id == 100
        assert merged.last_bot_reply_message_id == 101

    async def test_a_reply_that_answers_nothing_keeps_the_answered_watermark(self) -> None:
        """A hand-over line is the bot's message, but it answers no question."""
        db = _FakeDbManager(
            row=BedolagaTicketState(
                ticket_id=17,
                last_answered_message_id=100,
                last_bot_reply_message_id=101,
            )
        )
        await TicketStateStore(db).record_reply(17, 103)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_answered_message_id == 100
        assert merged.last_bot_reply_message_id == 103

    async def test_a_reply_that_answers_nothing_on_a_fresh_ticket(self) -> None:
        db = _FakeDbManager(row=None)
        await TicketStateStore(db).record_reply(17, 103)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_answered_message_id == 0
        assert merged.last_bot_reply_message_id == 103

    async def test_an_accepted_reply_without_an_id_preserves_the_previous_bot_id(self) -> None:
        db = _FakeDbManager(
            row=BedolagaTicketState(
                ticket_id=17,
                last_answered_message_id=100,
                last_bot_reply_message_id=101,
            )
        )
        await TicketStateStore(db).record_reply(17, None, answered_message_id=102)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_answered_message_id == 102
        assert merged.last_bot_reply_message_id == 101


class TestRecordHumanReply:
    """Recording a human operator's reply in Bedolaga panel."""

    async def test_record_human_reply_on_fresh_ticket(self) -> None:
        db = _FakeDbManager(row=None)
        await TicketStateStore(db).record_human_reply(17, 105)
        merged = db.session_obj.merge.await_args.args[0]
        assert isinstance(merged, BedolagaTicketState)
        assert merged.ticket_id == 17
        assert merged.last_human_reply_message_id == 105
        assert merged.last_human_reply_at is not None

    async def test_record_human_reply_on_existing_ticket(self) -> None:
        existing = BedolagaTicketState(
            ticket_id=17,
            last_answered_message_id=100,
            last_bot_reply_message_id=101,
        )
        db = _FakeDbManager(row=existing)
        await TicketStateStore(db).record_human_reply(17, 105)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_answered_message_id == 100
        assert merged.last_bot_reply_message_id == 101
        assert merged.last_human_reply_message_id == 105
        assert merged.last_human_reply_at is not None

    async def test_a_later_human_reply_advances_its_id_and_refreshes_the_timestamp(self) -> None:
        existing = BedolagaTicketState(
            ticket_id=17,
            last_answered_message_id=100,
            last_bot_reply_message_id=101,
            last_human_reply_message_id=105,
        )
        db = _FakeDbManager(row=existing)
        await TicketStateStore(db).record_human_reply(17, 108)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_human_reply_message_id == 108
        assert merged.last_human_reply_at is not None


class TestRecordMirroredMedia:
    """Recording media forwarding to operator topic."""

    async def test_fresh_ticket_record_mirrored_media(self) -> None:
        db = _FakeDbManager(row=None)
        await TicketStateStore(db).record_mirrored_media(17, 100)
        merged = db.session_obj.merge.await_args.args[0]
        assert isinstance(merged, BedolagaTicketState)
        assert merged.ticket_id == 17
        assert merged.last_mirrored_media_message_id == 100

    async def test_existing_ticket_advances_media_id(self) -> None:
        existing = BedolagaTicketState(
            ticket_id=17,
            last_answered_message_id=100,
            last_mirrored_media_message_id=90,
        )
        db = _FakeDbManager(row=existing)
        await TicketStateStore(db).record_mirrored_media(17, 105)
        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_mirrored_media_message_id == 105

    async def test_mirroring_the_pending_high_watermark_clears_the_retry(self) -> None:
        existing = BedolagaTicketState(
            ticket_id=17,
            last_answered_message_id=100,
            last_mirrored_media_message_id=90,
            pending_media_message_id=105,
        )
        db = _FakeDbManager(row=existing)

        await TicketStateStore(db).record_mirrored_media(17, 105)

        merged = db.session_obj.merge.await_args.args[0]
        assert merged.last_mirrored_media_message_id == 105
        assert merged.pending_media_message_id == 0

    async def test_media_already_mirrored_helper(self) -> None:
        db = _FakeDbManager(
            row=BedolagaTicketState(
                ticket_id=17,
                last_answered_message_id=0,
                last_mirrored_media_message_id=100,
            )
        )
        progress = await TicketStateStore(db).progress(17)
        assert progress.media_already_mirrored(100) is True
        assert progress.media_already_mirrored(99) is True
        assert progress.media_already_mirrored(101) is False


class TestPendingMedia:
    async def test_records_the_highest_pending_message(self) -> None:
        existing = BedolagaTicketState(
            ticket_id=17,
            last_answered_message_id=100,
            last_mirrored_media_message_id=90,
            pending_media_message_id=100,
        )
        db = _FakeDbManager(row=existing)

        await TicketStateStore(db).record_pending_media(17, 105)

        merged = db.session_obj.merge.await_args.args[0]
        assert merged.pending_media_message_id == 105

    async def test_lists_only_rows_returned_by_the_pending_query(self) -> None:
        db = _FakeDbManager()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [17, 23]
        db.session_obj.execute = AsyncMock(return_value=result)

        assert await TicketStateStore(db).pending_media_ticket_ids(10) == [17, 23]
