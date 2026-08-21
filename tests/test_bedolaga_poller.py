"""Unit tests for the reconciling sweep over open tickets."""

from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.poller import TicketPoller


def _poller(ids: list[int] | None = None, error: Exception | None = None):
    client = MagicMock()
    client.list_awaiting_ticket_ids = AsyncMock(
        side_effect=error,
        return_value=ids if ids is not None else [17, 18],
    )
    answerer = MagicMock()
    answerer.schedule = MagicMock()
    return TicketPoller(client=client, answerer=answerer), client, answerer


class TestSweep:
    """A webhook Bedolaga failed to deliver is never retried — this is the net."""

    async def test_schedules_every_open_ticket(self) -> None:
        poller, _, answerer = _poller()
        assert await poller.sweep() == 2
        assert [call.args[0] for call in answerer.schedule.call_args_list] == [17, 18]

    async def test_schedules_nothing_when_no_ticket_is_waiting(self) -> None:
        poller, _, answerer = _poller(ids=[])
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()

    async def test_passes_its_limit_to_the_client(self) -> None:
        poller, client, _ = _poller()
        poller.limit = 10
        await poller.sweep()
        client.list_awaiting_ticket_ids.assert_awaited_once_with(10)

    async def test_a_failing_sweep_reports_zero_instead_of_raising(self) -> None:
        poller, _, answerer = _poller(error=RuntimeError("panel down"))
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()
