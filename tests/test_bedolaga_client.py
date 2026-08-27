"""Unit tests for BedolagaClient — the only code that talks to the panel API."""

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.bedolaga.client import (
    MAX_MEDIA_BYTES,
    MAX_REPLY_LENGTH,
    BedolagaClient,
    PostedTicketReply,
)
from app.bedolaga.types import TELEGRAM_ID_UNKNOWN, TelegramIdLookup, TicketMedia

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


class _StreamContext:
    def __init__(self, response: MagicMock) -> None:
        self.response = response

    async def __aenter__(self) -> MagicMock:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


def _stream_response(
    content: bytes = b"binary-bytes",
    *,
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = headers if headers is not None else {"content-type": "image/png"}

    async def iter_chunks():
        for chunk in chunks if chunks is not None else [content]:
            yield chunk

    response.aiter_bytes = MagicMock(side_effect=iter_chunks)
    return response


def _client(
    get: Any = None,
    post: Any = None,
    stream: Any = None,
) -> tuple[BedolagaClient, MagicMock]:
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.get = get or AsyncMock(return_value=_response(200, TICKET_BODY))
    http_client.post = post or AsyncMock(return_value=_response(201, REPLY_BODY))
    http_client.stream = stream or MagicMock(return_value=_StreamContext(_stream_response()))
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

    async def test_survives_envelope_format_in_ticket_list(self) -> None:
        get = AsyncMock(
            side_effect=[
                _response(200, {"items": [{"id": 10}, {"id": 11}]}),
                _response(200, []),
            ]
        )
        client, _ = _client(get=get)
        assert sorted(await client.list_awaiting_ticket_ids()) == [10, 11]

    async def test_survives_invalid_json_in_ticket_list(self) -> None:
        bad_response = _response(200, None)
        bad_response.json = MagicMock(side_effect=ValueError("HTML error page"))
        get = AsyncMock(side_effect=[bad_response, _response(200, [{"id": 5}])])
        client, _ = _client(get=get)
        assert await client.list_awaiting_ticket_ids() == [5]

    async def test_filters_out_malformed_items_in_ticket_list(self) -> None:
        get = AsyncMock(
            side_effect=[
                _response(200, [{"id": 1}, "not-a-dict", {"id": "not-int"}, {"title": "no id"}]),
                _response(200, []),
            ]
        )
        client, _ = _client(get=get)
        assert await client.list_awaiting_ticket_ids() == [1]


class TestReply:
    """Posting the answer back into the ticket."""

    async def test_posts_the_text_and_returns_the_new_message_id(self) -> None:
        """The id is what lets the bot recognise its own reply later.

        Every message in a ticket written by support is `is_from_admin`, the
        bot's own included — so without this id an operator answering in the
        panel is indistinguishable from the bot itself.
        """
        client, http_client = _client()
        assert await client.reply(17, "Проверьте подписку") == PostedTicketReply(message_id=101)
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

    async def test_a_body_without_a_message_id_is_accepted_without_a_fake_id(self) -> None:
        client, _ = _client(post=AsyncMock(return_value=_response(201, {"status": "ok"})))
        assert await client.reply(17, "текст") == PostedTicketReply(message_id=None)

    async def test_a_body_that_is_not_json_is_accepted_without_a_fake_id(self) -> None:
        post = AsyncMock(return_value=_response(201, None))
        post.return_value.json = MagicMock(side_effect=ValueError("not json"))
        client, _ = _client(post=post)
        assert await client.reply(17, "текст") == PostedTicketReply(message_id=None)

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


class TestReplyWithPhoto:
    """The panel uploads bytes under its own Telegram token before attaching them."""

    async def test_uploads_then_attaches_a_photo_to_the_ticket(self) -> None:
        post = AsyncMock(
            side_effect=[
                _response(201, {"media_type": "photo", "file_id": "panel-file-id"}),
                _response(201, REPLY_BODY),
            ]
        )
        client, _ = _client(post=post)

        result = await client.reply_with_photo(
            17,
            "Вот настройки",
            base64.b64encode(b"image-bytes").decode("ascii"),
            "image/png",
        )

        assert result == PostedTicketReply(message_id=101)
        upload = post.await_args_list[0]
        assert upload.args[0] == "http://bedolaga:8080/upload"
        assert upload.kwargs["headers"] == {"X-API-Key": API_KEY}
        assert upload.kwargs["files"]["file"] == (
            "support-photo.png",
            b"image-bytes",
            "image/png",
        )
        assert upload.kwargs["data"] == {"media_type": "photo"}

        reply = post.await_args_list[1]
        assert reply.args[0] == "http://bedolaga:8080/tickets/17/reply"
        assert reply.kwargs["json"] == {
            "message_text": "Вот настройки",
            "media_type": "photo",
            "media_file_id": "panel-file-id",
            "media_caption": "Вот настройки",
        }

    async def test_does_not_post_a_reply_when_upload_fails(self) -> None:
        post = AsyncMock(return_value=_response(500, {}))
        client, _ = _client(post=post)

        result = await client.reply_with_photo(
            17,
            "",
            base64.b64encode(b"image-bytes").decode("ascii"),
            "image/jpeg",
        )

        assert result is None
        assert post.await_count == 1

    async def test_rejects_invalid_base64_before_calling_the_panel(self) -> None:
        post = AsyncMock()
        client, _ = _client(post=post)

        assert await client.reply_with_photo(17, "", "not-base64!", "image/jpeg") is None
        post.assert_not_awaited()


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


class TestDescribeMedia:
    """Describing ticket media attachments into safe transport-neutral descriptors."""

    async def test_describes_media_with_all_fields(self) -> None:
        media_response = _response(
            200,
            {
                "media_type": "video",
                "media_url": "http://bedolaga:8080/media/video.mp4",
                "file_name": "client.log",
                "mime_type": "video/mp4",
                "file_size": 2048,
            },
        )
        client, http_client = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100, "video")

        assert media is not None
        assert media.media_type == "video"
        assert media.media_url == "http://bedolaga:8080/media/video.mp4"
        assert media.filename == "client.log"
        assert media.mime_type == "video/mp4"
        assert media.file_size == 2048
        assert media.download_headers == {"X-API-Key": API_KEY}
        assert http_client.get.await_args.args[0] == "http://bedolaga:8080/tickets/17/messages/100/media"
        assert http_client.get.await_args.kwargs["headers"] == {"X-API-Key": API_KEY}

    async def test_accepts_filename_alias(self) -> None:
        media_response = _response(
            200,
            {
                "media_type": "document",
                "media_url": "http://bedolaga:8080/media/doc.pdf",
                "filename": "document.pdf",
            },
        )
        client, _ = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100)
        assert media is not None
        assert media.filename == "document.pdf"

    async def test_sanitizes_directory_traversal_and_paths_to_basename(self) -> None:
        media_response = _response(
            200,
            {
                "media_type": "document",
                "media_url": "http://bedolaga:8080/media/log",
                "file_name": "../../client.log",
            },
        )
        client, _ = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100)
        assert media is not None
        assert media.filename == "client.log"

    @pytest.mark.parametrize(
        ("raw_name", "media_type", "expected_filename"),
        [
            ("", "photo", "ticket-17-message-100.jpg"),
            ("   \n\t", "video", "ticket-17-message-100.mp4"),
            (None, "video_note", "ticket-17-message-100.mp4"),
            ("\x00\x01\x02", "animation", "ticket-17-message-100.gif"),
            (None, "voice", "ticket-17-message-100.ogg"),
            (None, "audio", "ticket-17-message-100.mp3"),
            (None, "document", "ticket-17-message-100.bin"),
            (None, "unknown_type", "ticket-17-message-100.bin"),
        ],
    )
    async def test_generates_safe_fallback_filename(
        self, raw_name: str | None, media_type: str, expected_filename: str
    ) -> None:
        payload: dict[str, Any] = {
            "media_type": media_type,
            "media_url": "http://bedolaga:8080/media/test",
        }
        if raw_name is not None:
            payload["file_name"] = raw_name
        client, _ = _client(get=AsyncMock(return_value=_response(200, payload)))
        media = await client.describe_media(17, 100)
        assert media is not None
        assert media.filename == expected_filename

    async def test_applies_fallback_media_type_when_payload_omits_media_type(self) -> None:
        media_response = _response(
            200,
            {"media_url": "http://bedolaga:8080/media/data"},
        )
        client, _ = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100, fallback_media_type="photo")
        assert media is not None
        assert media.media_type == "photo"

    async def test_defaults_media_type_to_document_when_both_omitted(self) -> None:
        media_response = _response(
            200,
            {"media_url": "http://bedolaga:8080/media/data"},
        )
        client, _ = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100, fallback_media_type=None)
        assert media is not None
        assert media.media_type == "document"

    async def test_resolves_relative_media_url(self) -> None:
        media_response = _response(200, {"media_url": "/media/file.mp4"})
        client, _ = _client(get=AsyncMock(return_value=media_response))
        media = await client.describe_media(17, 100)
        assert media is not None
        assert media.media_url == "http://bedolaga:8080/media/file.mp4"

    async def test_preserves_base_url_path_prefix_for_relative_media(self) -> None:
        media_response = _response(200, {"media_url": "media/file.mp4"})
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = AsyncMock(return_value=media_response)
        client = BedolagaClient("https://bedolaga/api", API_KEY, http_client)
        media = await client.describe_media(17, 100)
        assert media is not None
        assert media.media_url == "https://bedolaga/api/media/file.mp4"

    async def test_refuses_foreign_host(self) -> None:
        media_response = _response(200, {"media_url": "http://evil.example.com/steal"})
        client, _ = _client(get=AsyncMock(return_value=media_response))
        assert await client.describe_media(17, 100) is None

    async def test_refuses_https_to_http_downgrade(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga/file"})
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = AsyncMock(return_value=media_response)
        client = BedolagaClient("https://bedolaga", API_KEY, http_client)
        assert await client.describe_media(17, 100) is None

    async def test_refuses_different_port(self) -> None:
        media_response = _response(200, {"media_url": "https://bedolaga:9000/file"})
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = AsyncMock(return_value=media_response)
        client = BedolagaClient("https://bedolaga:8443", API_KEY, http_client)
        assert await client.describe_media(17, 100) is None

    async def test_returns_none_on_http_error(self) -> None:
        client, _ = _client(get=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.describe_media(17, 100) is None

    async def test_returns_none_on_non_200(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(404, {})))
        assert await client.describe_media(17, 100) is None

    async def test_returns_none_on_non_dict_json(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, ["not-a-dict"])))
        assert await client.describe_media(17, 100) is None

    async def test_returns_none_on_empty_or_null_media_url(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"media_url": ""})))
        assert await client.describe_media(17, 100) is None

    @pytest.mark.parametrize("invalid_size", [-1, -100, "not-a-number", "12.34"])
    async def test_returns_none_on_negative_or_invalid_file_size(self, invalid_size: Any) -> None:
        payload = {
            "media_type": "photo",
            "media_url": "http://bedolaga:8080/media/test.jpg",
            "file_size": invalid_size,
        }
        client, _ = _client(get=AsyncMock(return_value=_response(200, payload)))
        assert await client.describe_media(17, 100) is None


class TestDownloadImage:
    """Downloading vision attachments from TicketMedia descriptors."""

    async def test_downloads_and_encodes_image(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
            download_headers={"X-API-Key": API_KEY},
        )
        stream = MagicMock(return_value=_StreamContext(_stream_response(b"my-image-data")))
        client, http_client = _client(stream=stream)

        attachment = await client.download_image(media)
        assert attachment is not None
        assert attachment.base64_image == base64.b64encode(b"my-image-data").decode("ascii")
        assert attachment.mime_type == "image/png"
        assert http_client.stream.call_args.args[:2] == ("GET", "http://bedolaga:8080/media/photo.jpg")
        assert http_client.stream.call_args.kwargs["headers"] == {"X-API-Key": API_KEY}

    async def test_strips_parameters_from_content_type(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
            download_headers={"X-API-Key": API_KEY},
        )
        stream = MagicMock(
            return_value=_StreamContext(
                _stream_response(b"bytes", headers={"content-type": "image/jpeg; charset=utf-8"})
            )
        )
        client, _ = _client(stream=stream)
        attachment = await client.download_image(media)
        assert attachment is not None
        assert attachment.mime_type == "image/jpeg"

    async def test_defaults_mime_type_when_omitted(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
            download_headers={"X-API-Key": API_KEY},
        )
        stream = MagicMock(return_value=_StreamContext(_stream_response(b"bytes", headers={})))
        client, _ = _client(stream=stream)
        attachment = await client.download_image(media)
        assert attachment is not None
        assert attachment.mime_type == "image/jpeg"

    async def test_returns_none_on_empty_body(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
        )
        stream = MagicMock(return_value=_StreamContext(_stream_response(b"")))
        client, _ = _client(stream=stream)
        assert await client.download_image(media) is None

    async def test_returns_none_on_declared_oversize(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
        )
        streamed = _stream_response(headers={"content-length": str(MAX_MEDIA_BYTES + 1)})
        client, _ = _client(stream=MagicMock(return_value=_StreamContext(streamed)))
        assert await client.download_image(media) is None
        streamed.aiter_bytes.assert_not_called()

    async def test_returns_none_on_stream_exceeding_limit(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
        )
        oversized = _stream_response(
            chunks=[b"x" * (MAX_MEDIA_BYTES // 2), b"y" * (MAX_MEDIA_BYTES // 2 + 1)]
        )
        client, _ = _client(stream=MagicMock(return_value=_StreamContext(oversized)))
        assert await client.download_image(media) is None

    async def test_returns_none_on_http_error(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
        )
        client, _ = _client(stream=MagicMock(side_effect=httpx.ConnectError("refused")))
        assert await client.download_image(media) is None

    async def test_does_not_mutate_ticket_media(self) -> None:
        media = TicketMedia(
            media_type="photo",
            media_url="http://bedolaga:8080/media/photo.jpg",
            filename="photo.jpg",
            download_headers={"X-API-Key": API_KEY},
        )
        client, _ = _client(stream=MagicMock(return_value=_StreamContext(_stream_response(b"img"))))
        await client.download_image(media)
        assert media.filename == "photo.jpg"
        assert media.download_headers == {"X-API-Key": API_KEY}


class TestDownloadMedia:
    """A ticket screenshot lives behind the panel's own API key."""

    async def test_returns_the_encoded_image(self) -> None:
        media_response = _response(
            200, {"media_type": "photo", "media_url": "http://bedolaga:8080/media/abc"}
        )
        client, _ = _client(get=AsyncMock(return_value=media_response))

        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/png"
        assert attachment.base64_image == "YmluYXJ5LWJ5dGVz"

    async def test_defaults_the_mime_type_when_the_server_omits_it(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        stream = MagicMock(return_value=_StreamContext(_stream_response(b"x", headers={})))
        client, _ = _client(get=AsyncMock(return_value=media_response), stream=stream)
        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/jpeg"

    async def test_returns_none_without_a_media_url(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"media_url": None})))
        assert await client.download_media(17, 100) is None

    async def test_returns_none_when_the_download_fails(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        client, _ = _client(
            get=AsyncMock(return_value=media_response),
            stream=MagicMock(side_effect=httpx.ConnectError("no")),
        )
        assert await client.download_media(17, 100) is None

    async def test_resolves_a_relative_media_url_against_the_base_url(self) -> None:
        """httpx cannot request a bare path, and the panel plausibly returns one.

        Passing it through raised UnsupportedProtocol, which the handler below
        swallowed — screenshots would have silently stopped reaching the model.
        """
        media_response = _response(200, {"media_url": "/media/abc"})
        get = AsyncMock(return_value=media_response)
        stream = MagicMock(return_value=_StreamContext(_stream_response()))
        client, _ = _client(get=get, stream=stream)

        attachment = await client.download_media(17, 100)

        assert attachment is not None
        assert attachment.base64_image == "YmluYXJ5LWJ5dGVz"
        assert stream.call_args.args[:2] == ("GET", "http://bedolaga:8080/media/abc")

    async def test_refuses_a_media_url_on_a_foreign_host(self) -> None:
        """The API key goes to the configured panel and nowhere else.

        This is the only URL in the bot chosen by a remote response body, so a
        misconfigured or compromised panel must not be able to redirect the
        service token at a host of its choosing.
        """
        media_response = _response(200, {"media_url": "http://evil.example.com/steal"})
        get = AsyncMock(side_effect=[media_response])
        client, http_client = _client(get=get)

        assert await client.download_media(17, 100) is None
        assert get.await_count == 1
        http_client.stream.assert_not_called()

    async def test_refuses_https_to_http_downgrade(self) -> None:
        """Never send the API key over unencrypted HTTP when base_url is HTTPS."""
        media_response = _response(200, {"media_url": "http://bedolaga/file"})
        get = AsyncMock(side_effect=[media_response])
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = get
        client = BedolagaClient("https://bedolaga", API_KEY, http_client)

        assert await client.download_media(17, 100) is None
        # Must refuse without making the second GET to download the file
        assert get.await_count == 1

    async def test_refuses_different_effective_port(self) -> None:
        """Refuse media pointing to a different port on the same host."""
        media_response = _response(200, {"media_url": "https://bedolaga:9000/file"})
        get = AsyncMock(side_effect=[media_response])
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = get
        client = BedolagaClient("https://bedolaga:8443", API_KEY, http_client)

        assert await client.download_media(17, 100) is None
        assert get.await_count == 1

    async def test_preserves_base_url_path_prefix_for_relative_media(self) -> None:
        """Relative media paths must remain under base_url path prefix (e.g. /api)."""
        media_response = _response(200, {"media_url": "/media/abc"})
        get = AsyncMock(return_value=media_response)
        stream = MagicMock(return_value=_StreamContext(_stream_response()))
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = get
        http_client.stream = stream
        client = BedolagaClient("https://bedolaga/api", API_KEY, http_client)

        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert stream.call_args.args[:2] == ("GET", "https://bedolaga/api/media/abc")

    async def test_refuses_media_exceeding_max_size(self) -> None:
        """Never download/load into memory media exceeding MAX_MEDIA_BYTES."""
        media_response = _response(200, {"media_url": "/media/large.png"})
        oversized = _stream_response(
            chunks=[b"x" * (MAX_MEDIA_BYTES // 2), b"y" * (MAX_MEDIA_BYTES // 2 + 1)]
        )
        client, _ = _client(
            get=AsyncMock(return_value=media_response),
            stream=MagicMock(return_value=_StreamContext(oversized)),
        )

        assert await client.download_media(17, 100) is None

    async def test_rejects_declared_oversize_before_reading_a_chunk(self) -> None:
        media_response = _response(200, {"media_url": "/media/large.png"})
        streamed = _stream_response(headers={"content-length": str(MAX_MEDIA_BYTES + 1)})
        client, _ = _client(
            get=AsyncMock(return_value=media_response),
            stream=MagicMock(return_value=_StreamContext(streamed)),
        )

        assert await client.download_media(17, 100) is None
        streamed.aiter_bytes.assert_not_called()

