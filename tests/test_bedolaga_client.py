"""Unit tests for BedolagaClient — the only code that talks to the panel API."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.bedolaga.client import MAX_REPLY_LENGTH, BedolagaClient
from app.bedolaga.types import TELEGRAM_ID_UNKNOWN, TelegramIdLookup

BASE_URL = "http://bedolaga:8080"
API_KEY = "test-api-key"

TICKET_BODY: dict[str, Any] = {
    "id": 17,
    "user_id": 55,
    "title": "Не подключается",
    "status": "open",
    "priority": "normal",
    "messages": [{"id": 100, "message_text": "Помогите", "is_from_admin": False}],
}

#: What `POST /tickets/{id}/reply` answers with — a `TicketReplyResponse`
#: carrying the message the reply just became.
REPLY_BODY: dict[str, Any] = {
    "message": {"id": 101, "user_id": 55, "message_text": "текст", "is_from_admin": True}
}


def _response(status_code: int, json_body: Any = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.text = "body"
    response.headers = {}
    return response


def _client(
    get: Any = None,
    post: Any = None,
) -> tuple[BedolagaClient, MagicMock]:
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.get = get or AsyncMock(return_value=_response(200, TICKET_BODY))
    http_client.post = post or AsyncMock(return_value=_response(201, REPLY_BODY))
    return BedolagaClient(BASE_URL, API_KEY, http_client), http_client


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one call site that still retries must not spend its backoff here."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.retry._sleep", instant)


class TestGetTicket:
    """Reading one ticket, with its messages."""

    async def test_returns_the_parsed_ticket(self) -> None:
        client, http_client = _client()
        ticket = await client.get_ticket(17)
        assert ticket is not None
        assert ticket.id == 17
        assert ticket.messages[0].text == "Помогите"
        url = http_client.get.await_args.args[0]
        assert url == "http://bedolaga:8080/tickets/17"

    async def test_sends_the_api_key(self) -> None:
        client, http_client = _client()
        await client.get_ticket(17)
        assert http_client.get.await_args.kwargs["headers"]["X-API-Key"] == API_KEY

    async def test_returns_none_on_404(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(404, {})))
        assert await client.get_ticket(17) is None

    async def test_returns_none_when_the_panel_is_unreachable(self) -> None:
        client, _ = _client(get=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.get_ticket(17) is None

    async def test_trims_a_trailing_slash_from_the_base_url(self) -> None:
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = AsyncMock(return_value=_response(200, TICKET_BODY))
        client = BedolagaClient("http://bedolaga:8080/", API_KEY, http_client)
        await client.get_ticket(17)
        assert http_client.get.await_args.args[0] == "http://bedolaga:8080/tickets/17"


class TestListAwaitingTicketIds:
    """The list endpoint answers without messages, so it is good for ids only."""

    async def test_collects_ids_across_open_and_pending(self) -> None:
        get = AsyncMock(
            side_effect=[
                _response(200, [{"id": 1}, {"id": 2}]),
                _response(200, [{"id": 3}]),
            ]
        )
        client, _ = _client(get=get)
        assert sorted(await client.list_awaiting_ticket_ids()) == [1, 2, 3]

    async def test_survives_one_failing_status_query(self) -> None:
        get = AsyncMock(side_effect=[httpx.ConnectError("refused"), _response(200, [{"id": 3}])])
        client, _ = _client(get=get)
        assert await client.list_awaiting_ticket_ids() == [3]


class TestReply:
    """Posting the answer back into the ticket."""

    async def test_posts_the_text_and_returns_the_new_message_id(self) -> None:
        """The id is what lets the bot recognise its own reply later.

        Every message in a ticket written by support is `is_from_admin`, the
        bot's own included — so without this id an operator answering in the
        panel is indistinguishable from the bot itself.
        """
        client, http_client = _client()
        assert await client.reply(17, "Проверьте подписку") == 101
        url = http_client.post.await_args.args[0]
        assert url == "http://bedolaga:8080/tickets/17/reply"
        assert http_client.post.await_args.kwargs["json"] == {"message_text": "Проверьте подписку"}

    async def test_truncates_text_the_api_would_reject(self) -> None:
        client, http_client = _client()
        await client.reply(17, "я" * (MAX_REPLY_LENGTH + 500))
        sent = http_client.post.await_args.kwargs["json"]["message_text"]
        assert len(sent) == MAX_REPLY_LENGTH

    async def test_reports_failure_on_an_error_status(self) -> None:
        client, _ = _client(post=AsyncMock(return_value=_response(400, {})))
        assert await client.reply(17, "текст") is None

    async def test_reports_failure_when_the_panel_is_unreachable(self) -> None:
        client, _ = _client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.reply(17, "текст") is None

    async def test_a_body_without_a_message_id_does_not_raise(self) -> None:
        """A write that landed must never blow up over a parsing surprise.

        None here costs one admin alert and one backed-off retry; an exception
        would take out the whole turn, and the reply is already published.
        """
        client, _ = _client(post=AsyncMock(return_value=_response(201, {"status": "ok"})))
        assert await client.reply(17, "текст") is None

    async def test_a_body_that_is_not_json_does_not_raise(self) -> None:
        post = AsyncMock(return_value=_response(201, None))
        post.return_value.json = MagicMock(side_effect=ValueError("not json"))
        client, _ = _client(post=post)
        assert await client.reply(17, "текст") is None

    async def test_never_resends_a_gateway_error(self) -> None:
        """Posting a reply is not idempotent, so it must not go through a retry.

        The panel stores the message, flips the status and notifies the user
        before it answers; resending a write that may have landed duplicates
        all three. The ticket status is the retry: an unmarked message is
        re-read by the next sweep, which sees an admin reply if it did land.
        """
        post = AsyncMock(return_value=_response(500, {}))
        client, _ = _client(post=post)

        assert await client.reply(17, "текст") is None
        assert post.await_count == 1

    async def test_never_resends_after_a_timeout(self) -> None:
        post = AsyncMock(side_effect=httpx.ReadTimeout("too slow"))
        client, _ = _client(post=post)

        assert await client.reply(17, "текст") is None
        assert post.await_count == 1


class TestSetPriority:
    """Raising the priority is best effort — a failure must not lose the answer."""

    async def test_posts_the_priority(self) -> None:
        client, http_client = _client(post=AsyncMock(return_value=_response(200, {})))
        assert await client.set_priority(17, "high") is True
        assert http_client.post.await_args.args[0] == "http://bedolaga:8080/tickets/17/priority"
        assert http_client.post.await_args.kwargs["json"] == {"priority": "high"}

    async def test_returns_false_instead_of_raising(self) -> None:
        client, _ = _client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.set_priority(17, "high") is False

    async def test_still_retries_a_gateway_error(self) -> None:
        """Raising a priority twice is harmless, so this one keeps its retry."""
        post = AsyncMock(return_value=_response(500, {}))
        client, _ = _client(post=post)

        assert await client.set_priority(17, "high") is False
        assert post.await_count == 3


class TestResolveTelegramId:
    """A ticket carries the panel's own user id, never a Telegram one."""

    async def test_reads_the_telegram_id_of_the_panel_user(self) -> None:
        client, http_client = _client(
            get=AsyncMock(return_value=_response(200, {"telegram_id": 42}))
        )
        lookup = await client.resolve_telegram_id(55)
        assert lookup == TelegramIdLookup(known=True, telegram_id=42)
        assert http_client.get.await_args.args[0] == "http://bedolaga:8080/users/55"

    async def test_a_cabinet_only_user_is_a_known_answer_without_an_id(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"telegram_id": None})))
        lookup = await client.resolve_telegram_id(55)
        assert lookup.known is True
        assert lookup.telegram_id is None

    async def test_an_error_status_is_not_the_same_as_no_telegram_id(self) -> None:
        """A one-second 502 must not read as "this account has no Telegram".

        The caller turns "no Telegram id" into a negative synthetic key that a
        forum topic and a chat history then hang off permanently, so the two
        outcomes may never share a value.
        """
        client, _ = _client(get=AsyncMock(return_value=_response(502, {})))
        assert await client.resolve_telegram_id(55) == TELEGRAM_ID_UNKNOWN

    async def test_a_transport_error_is_not_the_same_as_no_telegram_id(self) -> None:
        client, _ = _client(get=AsyncMock(side_effect=httpx.ReadTimeout("too slow")))
        assert await client.resolve_telegram_id(55) == TELEGRAM_ID_UNKNOWN

    async def test_caches_the_lookup(self) -> None:
        get = AsyncMock(return_value=_response(200, {"telegram_id": 42}))
        client, _ = _client(get=get)
        await client.resolve_telegram_id(55)
        await client.resolve_telegram_id(55)
        assert get.await_count == 1

    async def test_does_not_cache_a_failed_lookup(self) -> None:
        get = AsyncMock(side_effect=[_response(500, {}), _response(200, {"telegram_id": 42})])
        client, _ = _client(get=get)
        assert (await client.resolve_telegram_id(55)).known is False
        assert (await client.resolve_telegram_id(55)).telegram_id == 42


class TestDownloadMedia:
    """A ticket screenshot lives behind the panel's own API key."""

    async def test_returns_the_encoded_image(self) -> None:
        media_response = _response(
            200, {"media_type": "photo", "media_url": "http://bedolaga:8080/media/abc"}
        )
        file_response = MagicMock(spec=httpx.Response)
        file_response.status_code = 200
        file_response.content = b"binary-bytes"
        file_response.headers = {"content-type": "image/png"}
        client, _ = _client(get=AsyncMock(side_effect=[media_response, file_response]))

        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/png"
        assert attachment.base64_image == "YmluYXJ5LWJ5dGVz"

    async def test_defaults_the_mime_type_when_the_server_omits_it(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        file_response = MagicMock(spec=httpx.Response)
        file_response.status_code = 200
        file_response.content = b"x"
        file_response.headers = {}
        client, _ = _client(get=AsyncMock(side_effect=[media_response, file_response]))
        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/jpeg"

    async def test_returns_none_without_a_media_url(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"media_url": None})))
        assert await client.download_media(17, 100) is None

    async def test_returns_none_when_the_download_fails(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        client, _ = _client(get=AsyncMock(side_effect=[media_response, httpx.ConnectError("no")]))
        assert await client.download_media(17, 100) is None

    async def test_resolves_a_relative_media_url_against_the_base_url(self) -> None:
        """httpx cannot request a bare path, and the panel plausibly returns one.

        Passing it through raised UnsupportedProtocol, which the handler below
        swallowed — screenshots would have silently stopped reaching the model.
        """
        media_response = _response(200, {"media_url": "/media/abc"})
        file_response = MagicMock(spec=httpx.Response)
        file_response.status_code = 200
        file_response.content = b"binary-bytes"
        file_response.headers = {"content-type": "image/png"}
        get = AsyncMock(side_effect=[media_response, file_response])
        client, _ = _client(get=get)

        attachment = await client.download_media(17, 100)

        assert attachment is not None
        assert attachment.base64_image == "YmluYXJ5LWJ5dGVz"
        assert get.await_args_list[1].args[0] == "http://bedolaga:8080/media/abc"

    async def test_refuses_a_media_url_on_a_foreign_host(self) -> None:
        """The API key goes to the configured panel and nowhere else.

        This is the only URL in the bot chosen by a remote response body, so a
        misconfigured or compromised panel must not be able to redirect the
        service token at a host of its choosing.
        """
        media_response = _response(200, {"media_url": "http://evil.example.com/steal"})
        get = AsyncMock(side_effect=[media_response])
        client, _ = _client(get=get)

        assert await client.download_media(17, 100) is None
        assert get.await_count == 1
