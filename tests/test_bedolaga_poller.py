"""Unit tests for the reconciling sweep over open tickets."""

from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.poller import TicketPoller


def _poller(
    ids: list[int] | None = None,
    error: Exception | None = None,
    pending_media_ids: list[int] | None = None,
):
    client = MagicMock()
    client.list_awaiting_ticket_ids = AsyncMock(
        side_effect=error,
        return_value=ids if ids is not None else [17, 18],
    )
    answerer = MagicMock()
    answerer.schedule = MagicMock()
    state = MagicMock()
    state.pending_media_ticket_ids = AsyncMock(return_value=pending_media_ids or [])
    return TicketPoller(client=client, answerer=answerer, state=state), client, answerer, state


class TestSweep:
    """The sweep reconciles missed webhooks and durable media retries."""

    async def test_schedules_every_open_ticket(self) -> None:
        poller, _, answerer, _ = _poller()
        assert await poller.sweep() == 2
        assert [call.args[0] for call in answerer.schedule.call_args_list] == [17, 18]

    async def test_schedules_nothing_when_no_ticket_is_waiting(self) -> None:
        poller, _, answerer, _ = _poller(ids=[])
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()

    async def test_passes_its_limit_to_the_client(self) -> None:
        poller, client, _, state = _poller()
        poller.limit = 10
        await poller.sweep()
        client.list_awaiting_ticket_ids.assert_awaited_once_with(10)
        state.pending_media_ticket_ids.assert_awaited_once_with(10)

    async def test_a_failing_sweep_reports_zero_instead_of_raising(self) -> None:
        poller, _, answerer, _ = _poller(error=RuntimeError("panel down"))
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()

    async def test_schedules_answered_ticket_with_pending_media(self) -> None:
        poller, _, answerer, _ = _poller(ids=[], pending_media_ids=[17])

        assert await poller.sweep() == 1
        answerer.schedule.assert_called_once_with(17)

    async def test_deduplicates_open_and_pending_media_ticket(self) -> None:
        poller, _, answerer, _ = _poller(ids=[17], pending_media_ids=[17])

        assert await poller.sweep() == 1
        answerer.schedule.assert_called_once_with(17)
