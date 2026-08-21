"""The Bedolaga pieces driven together, rather than each against the next one's mock.

Every module in `app/bedolaga/` is unit tested on its own, which proves each one
calls its neighbour correctly and nothing about the assembled chain. These two
tests cover what only the whole thing can show: that a delivery really does
travel webhook → schedule → pipeline → client → panel, and that the per-ticket
lock really does stop two concurrent turns from answering the same ticket twice.
"""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from app.bedolaga.client import BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.state import TicketStateStore
from app.bedolaga.types import Ticket, TicketMessage
from app.bedolaga.webhook import BedolagaWebhookEndpoint
from app.bot.conversation_state import ConversationState
from app.bot.rate_limiter import UserRateLimiter
from app.llm.base import LlmReply
from app.storage.models import BedolagaTicketState

BASE_URL = "http://bedolaga.test:8080"
API_KEY = "service-token"
SECRET = "webhook-secret"
TICKET_ID = 17
PANEL_USER_ID = 55
TELEGRAM_ID = 42
ANSWER = "Проверьте подписку в личном кабинете."

TICKET_BODY: dict[str, Any] = {
    "id": TICKET_ID,
    "user_id": PANEL_USER_ID,
    "title": "Не подключается",
    "status": "open",
    "priority": "normal",
    "messages": [{"id": 100, "message_text": "Помогите", "is_from_admin": False}],
}


class _FakeDatabase:
    """Stands in for the session manager so TicketStateStore's own code runs.

    The store is real here — only the rows it reads and writes are in memory.
    """

    def __init__(self) -> None:
        self.rows: dict[int, BedolagaTicketState] = {}

    @asynccontextmanager
    async def session(self) -> Any:
        rows = self.rows

        async def get(_model: Any, key: int) -> BedolagaTicketState | None:
            return rows.get(key)

        async def merge(obj: BedolagaTicketState) -> BedolagaTicketState:
            rows[obj.ticket_id] = obj
            return obj

        session = MagicMock()
        session.get = AsyncMock(side_effect=get)
        session.merge = AsyncMock(side_effect=merge)
        yield session


def _panel(recorded: list[httpx.Request]) -> httpx.MockTransport:
    """The Bedolaga panel, as HTTP — the only boundary this test mocks."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        if request.method == "GET" and path == f"/tickets/{TICKET_ID}":
            return httpx.Response(200, json=TICKET_BODY)
        if request.method == "GET" and path == f"/users/{PANEL_USER_ID}":
            return httpx.Response(200, json={"telegram_id": TELEGRAM_ID})
        if request.method == "POST" and path == f"/tickets/{TICKET_ID}/reply":
            return httpx.Response(201, json={"status": "ok"})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _webhook_request(payload: dict[str, Any], event: str) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = MagicMock()
    request.read = AsyncMock(return_value=body)
    request.headers = {
        "X-Webhook-Event": event,
        "X-Webhook-Signature": "sha256="
        + hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest(),
    }
    return request


class TestWebhookToPanel:
    """One delivery, all the way to the reply the panel receives."""

    async def test_a_ticket_created_event_produces_a_reply_on_the_panel(self) -> None:
        recorded: list[httpx.Request] = []
        llm_client = MagicMock()
        llm_client.supports_images = MagicMock(return_value=False)
        llm_client.chat = AsyncMock(return_value=LlmReply(text=ANSWER))
        forwarder = MagicMock()
        forwarder.forward_to_support = AsyncMock()
        knowledge_gap_service = MagicMock()
        knowledge_gap_service.evaluate = AsyncMock()
        admin_notifier = MagicMock()
        admin_notifier.notify_error = AsyncMock()

        async with httpx.AsyncClient(transport=_panel(recorded)) as http_client:
            answerer = TicketAnswerer(
                client=BedolagaClient(BASE_URL, API_KEY, http_client),
                llm_client=llm_client,
                state=TicketStateStore(_FakeDatabase()),
                rate_limiter=UserRateLimiter(),
                admin_notifier=admin_notifier,
                forwarder=forwarder,
                knowledge_gap_service=knowledge_gap_service,
                conversation_state=ConversationState(),
            )
            endpoint = BedolagaWebhookEndpoint(answerer=answerer, secret=SECRET)

            response = await endpoint.handle(
                _webhook_request({"ticket_id": TICKET_ID}, "ticket.created")
            )
            assert response.status == 200

            await answerer.drain()

        admin_notifier.notify_error.assert_not_awaited()

        posted = [r for r in recorded if r.method == "POST"]
        assert len(posted) == 1
        assert posted[0].url.path == f"/tickets/{TICKET_ID}/reply"
        assert json.loads(posted[0].content) == {"message_text": ANSWER}
        assert posted[0].headers["X-API-Key"] == API_KEY

        # The whole chain ran: the ticket was read in full, the panel user was
        # resolved to a Telegram id, and the model was asked under that id.
        read_paths = [r.url.path for r in recorded if r.method == "GET"]
        assert f"/tickets/{TICKET_ID}" in read_paths
        assert f"/users/{PANEL_USER_ID}" in read_paths
        assert llm_client.chat.await_args.args[1] == TELEGRAM_ID

    async def test_a_delivery_with_a_bad_signature_reaches_nothing(self) -> None:
        recorded: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_panel(recorded)) as http_client:
            answerer = TicketAnswerer(
                client=BedolagaClient(BASE_URL, API_KEY, http_client),
                llm_client=MagicMock(),
                state=TicketStateStore(_FakeDatabase()),
                rate_limiter=UserRateLimiter(),
                admin_notifier=MagicMock(),
                forwarder=MagicMock(),
                knowledge_gap_service=MagicMock(),
                conversation_state=ConversationState(),
            )
            endpoint = BedolagaWebhookEndpoint(answerer=answerer, secret=SECRET)

            request = _webhook_request({"ticket_id": TICKET_ID}, "ticket.created")
            request.headers = dict(request.headers) | {"X-Webhook-Signature": "sha256=deadbeef"}
            response = await endpoint.handle(request)

            assert response.status == 403
            await answerer.drain()

        assert recorded == []


class _InMemoryState:
    """What TicketStateStore records, without a database behind it."""

    def __init__(self) -> None:
        self.answered: dict[int, int] = {}

    async def already_answered(self, ticket_id: int, message_id: int) -> bool:
        return self.answered.get(ticket_id, -1) >= message_id

    async def mark_answered(self, ticket_id: int, message_id: int) -> None:
        self.answered[ticket_id] = message_id


class TestConcurrentTurns:
    """A webhook and a sweep can bring in the same ticket a millisecond apart."""

    async def test_two_concurrent_turns_answer_the_ticket_once(self) -> None:
        ticket = Ticket(
            id=TICKET_ID,
            user_id=PANEL_USER_ID,
            title="Не подключается",
            status="open",
            messages=(TicketMessage(id=100, text="Помогите", is_from_admin=False),),
        )

        # Both mocks yield to the event loop, the way a real HTTP call and a
        # real model turn do. Without a suspension point the first turn would
        # run to completion before the second one started, and this test would
        # pass with no lock at all.
        async def read_ticket(_ticket_id: int) -> Ticket:
            await asyncio.sleep(0)
            return ticket

        async def ask(_question: str, _user_id: int) -> LlmReply:
            await asyncio.sleep(0)
            return LlmReply(text=ANSWER)

        client = MagicMock()
        client.get_ticket = AsyncMock(side_effect=read_ticket)
        client.resolve_telegram_id = AsyncMock(return_value=TELEGRAM_ID)
        client.reply = AsyncMock(return_value=True)
        client.set_priority = AsyncMock(return_value=True)

        llm_client = MagicMock()
        llm_client.supports_images = MagicMock(return_value=False)
        llm_client.chat = AsyncMock(side_effect=ask)
        forwarder = MagicMock()
        forwarder.forward_to_support = AsyncMock()
        knowledge_gap_service = MagicMock()
        knowledge_gap_service.evaluate = AsyncMock()
        admin_notifier = MagicMock()
        admin_notifier.notify_error = AsyncMock()

        state = _InMemoryState()
        answerer = TicketAnswerer(
            client=client,
            llm_client=llm_client,
            state=state,  # type: ignore[arg-type]
            # No interval, so the rate limiter cannot be what stops the second
            # turn — the per-ticket lock has to be, which is the whole point.
            rate_limiter=UserRateLimiter(min_interval=0.0),
            admin_notifier=admin_notifier,
            forwarder=forwarder,
            knowledge_gap_service=knowledge_gap_service,
            conversation_state=ConversationState(),
        )

        await asyncio.gather(answerer.handle(TICKET_ID), answerer.handle(TICKET_ID))

        # Without the per-ticket lock both turns read the ticket before either
        # recorded an answer, and the user got the same reply twice — plus two
        # Telegram notifications from the panel.
        client.reply.assert_awaited_once()
        assert state.answered == {TICKET_ID: 100}
        assert llm_client.chat.await_count == 1
        assert answerer._tickets.active_keys() == 0
