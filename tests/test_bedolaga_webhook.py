"""Unit tests for the Bedolaga webhook endpoint."""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from app.bedolaga.webhook import BedolagaWebhookEndpoint, signature_matches

SECRET = "webhook-secret"


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _request(body: bytes, event: str, signature: str | None = None) -> Any:
    request = MagicMock()
    request.read = AsyncMock(return_value=body)
    headers = {"X-Webhook-Event": event}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature
    request.headers = headers
    return request


def _endpoint(secret: str = SECRET) -> tuple[BedolagaWebhookEndpoint, MagicMock]:
    answerer = MagicMock()
    answerer.schedule = MagicMock()
    return BedolagaWebhookEndpoint(answerer=answerer, secret=secret), answerer


class TestSignatureMatches:
    """The endpoint is reachable from outside; the secret is what guards it."""

    def test_accepts_a_correct_signature(self) -> None:
        body = _body({"ticket_id": 17})
        assert signature_matches(SECRET, body, _signature(body)) is True

    def test_rejects_a_signature_from_another_secret(self) -> None:
        body = _body({"ticket_id": 17})
        assert signature_matches(SECRET, body, _signature(body, "other")) is False

    def test_rejects_a_missing_signature(self) -> None:
        assert signature_matches(SECRET, _body({}), None) is False

    def test_accepts_anything_when_no_secret_is_configured(self) -> None:
        assert signature_matches("", _body({}), None) is True


class TestHandle:
    """What each kind of delivery does."""

    async def test_schedules_the_ticket_on_a_new_ticket(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "user_id": 55, "title": "t"})
        response = await endpoint.handle(_request(body, "ticket.created", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_called_once_with(17)

    async def test_schedules_the_ticket_on_a_user_message(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "message_id": 101, "is_from_admin": False})
        response = await endpoint.handle(_request(body, "ticket.message_added", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_called_once_with(17)

    async def test_ignores_our_own_reply_coming_back(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "message_id": 102, "is_from_admin": True})
        response = await endpoint.handle(_request(body, "ticket.message_added", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_not_called()

    async def test_ignores_an_unrelated_event(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "new_status": "closed"})
        response = await endpoint.handle(_request(body, "ticket.status_changed", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_not_called()

    async def test_rejects_a_bad_signature(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17})
        response = await endpoint.handle(_request(body, "ticket.created", _signature(body, "other")))
        assert response.status == 403
        answerer.schedule.assert_not_called()

    async def test_rejects_a_body_that_is_not_json(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(b"not json", "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()

    async def test_rejects_a_payload_without_a_ticket_id(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(_body({"user_id": 55}), "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()

    async def test_rejects_a_json_body_that_is_not_a_dict(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(b"[1,2,3]", "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()

    async def test_rejects_a_non_numeric_ticket_id(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(_body({"ticket_id": "abc", "user_id": 55}), "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()


class TestRegister:
    """The endpoint hangs off the healthcheck server the bot already runs."""

    def test_adds_a_post_route(self) -> None:
        endpoint, _ = _endpoint()
        app = web.Application()
        endpoint.register(app, "/bedolaga/webhook")
        routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
        assert ("POST", "/bedolaga/webhook") in routes
