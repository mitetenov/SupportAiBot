"""Receives Bedolaga ticket events on the bot's own HTTP server."""

import hashlib
import hmac
import json
import logging

from aiohttp import web

from app.bedolaga.pipeline import TicketAnswerer

logger = logging.getLogger(__name__)


def signature_matches(secret: str, body: bytes, header: str | None) -> bool:
    """Verify the `X-Webhook-Signature` Bedolaga sends over the raw body.

    With no secret configured there is nothing to check — the endpoint is then
    only as safe as the network it listens on, which is why the README tells
    you to set one.
    """
    if not secret:
        return True
    if not header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    supplied = header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, supplied)


class BedolagaWebhookEndpoint:
    """Turns a ticket event into scheduled work and answers immediately."""

    #: `ticket.status_changed` is delivered too, but a status change on its own
    #: never means somebody is waiting for an answer.
    HANDLED_EVENTS: frozenset[str] = frozenset({"ticket.created", "ticket.message_added"})

    def __init__(self, answerer: TicketAnswerer, secret: str = "") -> None:
        self.answerer = answerer
        self.secret = secret

    def register(self, app: web.Application, path: str) -> None:
        """Mount the endpoint on the aiohttp app that already serves /health."""
        app.router.add_post(path, self.handle)
        logger.info("Bedolaga webhook endpoint registered at %s", path)

    async def handle(self, request: web.Request) -> web.Response:
        """Accept one delivery. Never does the work inline.

        Bedolaga gives a webhook ten seconds and does not retry a failure, so
        this returns as soon as the event is understood; the answer happens on
        a background task.
        """
        body = await request.read()

        if not signature_matches(self.secret, body, request.headers.get("X-Webhook-Signature")):
            logger.warning("Bedolaga webhook: rejected a delivery with a bad signature")
            return web.json_response({"status": "forbidden"}, status=403)

        event = request.headers.get("X-Webhook-Event", "")
        if event not in self.HANDLED_EVENTS:
            return web.json_response({"status": "ignored"})

        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Bedolaga webhook: body was not JSON")
            return web.json_response({"status": "bad request"}, status=400)

        if payload.get("is_from_admin"):
            # Our own reply is stored as an admin message and comes straight
            # back as an event. Answering it would answer ourselves, forever.
            return web.json_response({"status": "ignored"})

        ticket_id = payload.get("ticket_id")
        if ticket_id is None:
            logger.warning("Bedolaga webhook: %s carried no ticket_id", event)
            return web.json_response({"status": "bad request"}, status=400)

        self.answerer.schedule(int(ticket_id))
        return web.json_response({"status": "accepted"})
