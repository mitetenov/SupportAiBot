"""Unit tests for TelegramMessageSender: chunking, blank handling, error containment."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bedolaga.types import TicketMedia
from app.bot.sender import MAX_MESSAGE_LENGTH, TelegramMessageSender


def make_sender() -> tuple[TelegramMessageSender, MagicMock]:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock(return_value=MagicMock(message_id=77))
    bot.set_message_reaction = AsyncMock()
    return TelegramMessageSender(bot), bot


class TestSplit:
    def test_short_text_is_one_chunk(self) -> None:
        assert TelegramMessageSender.split("привет") == ["привет"]

    def test_text_at_the_limit_is_not_split(self) -> None:
        text = "x" * MAX_MESSAGE_LENGTH
        assert TelegramMessageSender.split(text) == [text]

    def test_every_chunk_stays_within_the_limit(self) -> None:
        text = "y" * (MAX_MESSAGE_LENGTH * 3 + 17)
        chunks = TelegramMessageSender.split(text)
        assert len(chunks) == 4
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)

    def test_splits_on_newlines_and_round_trips(self) -> None:
        source = "\n".join(f"строка {i} " + "z" * 80 for i in range(200))
        chunks = TelegramMessageSender.split(source)
        assert len(chunks) > 1
        assert all(len(c) <= MAX_MESSAGE_LENGTH for c in chunks)

    def test_a_single_unbroken_run_is_cut_at_the_limit(self) -> None:
        text = "q" * (MAX_MESSAGE_LENGTH + 1)
        assert TelegramMessageSender.split(text) == ["q" * MAX_MESSAGE_LENGTH, "q"]


class TestSend:
    @pytest.mark.asyncio
    async def test_sends_long_text_as_several_messages(self) -> None:
        sender, bot = make_sender()
        await sender.send(100, "a" * (MAX_MESSAGE_LENGTH + 500))
        assert bot.send_message.await_count == 2
        for call in bot.send_message.await_args_list:
            assert len(call.kwargs["text"]) <= MAX_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_short_send_keeps_the_plain_call_shape(self) -> None:
        sender, bot = make_sender()
        await sender.send(100, "готово")
        bot.send_message.assert_awaited_once_with(chat_id=100, text="готово", parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_send_chunks_with_html_formatting(self) -> None:
        sender, bot = make_sender()
        await sender.send(100, "**Важное сообщение**")
        bot.send_message.assert_awaited_once_with(
            chat_id=100,
            text="<b>Важное сообщение</b>",
            parse_mode="HTML",
        )

    @pytest.mark.asyncio
    async def test_send_long_markdown_preserves_formatting_across_chunks(self) -> None:
        sender, bot = make_sender()
        long_body = "x" * (MAX_MESSAGE_LENGTH + 100)
        await sender.send(100, f"**{long_body}**")
        assert bot.send_message.await_count == 2
        first_call = bot.send_message.await_args_list[0].kwargs
        second_call = bot.send_message.await_args_list[1].kwargs
        assert first_call["parse_mode"] == "HTML"
        assert first_call["text"].startswith("<b>") and first_call["text"].endswith("</b>")
        assert second_call["parse_mode"] == "HTML"
        assert second_call["text"].startswith("<b>") and second_call["text"].endswith("</b>")

    @pytest.mark.asyncio
    async def test_send_chunks_fallback_on_parse_error(self) -> None:
        sender, bot = make_sender()
        bot.send_message.side_effect = [
            TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities"),
            None,
        ]
        await sender.send(100, "**Некорректный тег**")
        assert bot.send_message.await_count == 2
        # First attempt: HTML formatted
        first_call = bot.send_message.await_args_list[0].kwargs
        assert first_call["parse_mode"] == "HTML"
        assert first_call["text"] == "<b>Некорректный тег</b>"
        # Second attempt: fallback with plain text and parse_mode=None
        second_call = bot.send_message.await_args_list[1].kwargs
        assert second_call["parse_mode"] is None
        assert second_call["text"] == "Некорректный тег"

    @pytest.mark.asyncio
    async def test_send_chunks_no_retry_on_network_or_blocked_error(self) -> None:
        sender, bot = make_sender()
        bot.send_message.side_effect = RuntimeError("bot was blocked by the user")
        await sender.send(100, "сообщение")
        # Must NOT retry
        assert bot.send_message.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", [None, "", "   ", "\n\t"])
    async def test_blank_text_is_never_sent(self, blank: str | None) -> None:
        sender, bot = make_sender()
        await sender.send(100, blank)
        bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_send_does_not_raise(self) -> None:
        sender, bot = make_sender()
        bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked by the user"))
        await sender.send(100, "текст")
        assert bot.send_message.await_count == 1

    @pytest.mark.asyncio
    async def test_only_the_first_chunk_carries_the_reply_link(self) -> None:
        sender, bot = make_sender()
        await sender.send_reply(100, 55, "b" * (MAX_MESSAGE_LENGTH + 10))
        calls = bot.send_message.await_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["reply_to_message_id"] == 55
        assert "reply_to_message_id" not in calls[1].kwargs

    @pytest.mark.asyncio
    async def test_topic_send_carries_the_thread_id(self) -> None:
        sender, bot = make_sender()
        await sender.send_to_topic(-100123, 42, "в топик")
        bot.send_message.assert_awaited_once_with(
            chat_id=-100123, message_thread_id=42, text="в топик", parse_mode="HTML"
        )


class TestCopyAndReact:
    @pytest.mark.asyncio
    async def test_copy_returns_the_new_message_id(self) -> None:
        sender, bot = make_sender()
        assert await sender.copy_message(chat_id=1, from_chat_id=2, message_id=3) == 77

    @pytest.mark.asyncio
    async def test_copy_returns_none_when_it_fails(self) -> None:
        sender, bot = make_sender()
        bot.copy_message = AsyncMock(side_effect=RuntimeError("chat not found"))
        assert await sender.copy_message(chat_id=1, from_chat_id=2, message_id=3) is None

    @pytest.mark.asyncio
    async def test_reaction_failure_is_contained(self) -> None:
        sender, bot = make_sender()
        bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("message not found"))
        await sender.set_reaction(1, 2, [])
        bot.set_message_reaction.assert_awaited_once()


class TestSendPhoto:
    """Sending an illustration must never be able to cost the user their answer."""

    @pytest.mark.asyncio
    async def test_uploads_the_file_and_returns_the_message_id(self, tmp_path) -> None:
        picture = tmp_path / "happ-buttons.png"
        picture.write_bytes(b"\x89PNG fake")

        sender, bot = make_sender()
        bot.send_photo = AsyncMock(return_value=MagicMock(message_id=501, photo=[]))

        message_id = await sender.send_photo(100, picture)

        assert message_id == 501
        assert bot.send_photo.await_args.kwargs["chat_id"] == 100

    @pytest.mark.asyncio
    async def test_reuses_the_file_id_telegram_returned_instead_of_uploading_again(
        self, tmp_path
    ) -> None:
        picture = tmp_path / "happ-buttons.png"
        picture.write_bytes(b"\x89PNG fake")

        sender, bot = make_sender()
        uploaded = MagicMock(message_id=501, photo=[MagicMock(file_id="FILE_ID_FROM_TELEGRAM")])
        bot.send_photo = AsyncMock(return_value=uploaded)

        await sender.send_photo(100, picture)
        await sender.send_photo(200, picture)

        assert bot.send_photo.await_args.kwargs["photo"] == "FILE_ID_FROM_TELEGRAM"

    @pytest.mark.asyncio
    async def test_a_missing_file_is_skipped_rather_than_raised(self, tmp_path) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock()

        assert await sender.send_photo(100, tmp_path / "not-there.png") is None
        bot.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_send_is_swallowed(self, tmp_path) -> None:
        picture = tmp_path / "happ-buttons.png"
        picture.write_bytes(b"\x89PNG fake")

        sender, bot = make_sender()
        bot.send_photo = AsyncMock(side_effect=RuntimeError("flood wait"))

        assert await sender.send_photo(100, picture) is None

    @pytest.mark.asyncio
    async def test_uploads_in_memory_ticket_photo_to_a_topic(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock(return_value=MagicMock(message_id=601))

        result = await sender.send_photo_bytes(
            -100123,
            base64.b64encode(b"png-bytes").decode("ascii"),
            "image/png",
            message_thread_id=42,
            caption="Тикет #17",
        )

        assert result == 601
        sent = bot.send_photo.await_args.kwargs
        assert sent["chat_id"] == -100123
        assert sent["message_thread_id"] == 42
        assert sent["caption"] == "Тикет #17"
        assert sent["photo"].filename == "ticket-photo.png"

    @pytest.mark.asyncio
    async def test_rejects_invalid_in_memory_photo(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock()

        assert await sender.send_photo_bytes(1, "bad!", "image/jpeg") is None
        bot.send_photo.assert_not_awaited()


class TestSendTicketMedia:

    """Streaming ticket media from Bedolaga to Telegram operator topics."""

    @pytest.mark.asyncio
    async def test_photo_sends_via_send_photo(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock(return_value=MagicMock(message_id=701))

        media = TicketMedia(
            media_type="photo",
            media_url="https://bedolaga/media/test.jpg",
            filename="screenshot.jpg",
            mime_type="image/jpeg",
            download_headers={"X-API-Key": "secret-key"},
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
            message_thread_id=42,
            caption="Ticket #17",
        )

        assert result == 701
        bot.send_photo.assert_awaited_once()
        sent = bot.send_photo.await_args.kwargs
        assert sent["chat_id"] == -100123
        assert sent["message_thread_id"] == 42
        assert sent["caption"] == "Ticket #17"
        input_file = sent["photo"]
        assert input_file.url == "https://bedolaga/media/test.jpg"
        assert input_file.filename == "screenshot.jpg"
        assert input_file.headers == {"X-API-Key": "secret-key"}
        assert input_file.timeout == 120

    @pytest.mark.asyncio
    async def test_mp4_video_sends_via_send_video_with_streaming(self) -> None:
        sender, bot = make_sender()
        bot.send_video = AsyncMock(return_value=MagicMock(message_id=702))

        media = TicketMedia(
            media_type="video",
            media_url="https://bedolaga/media/test.mp4",
            filename="record.mp4",
            mime_type="video/mp4",
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
            message_thread_id=42,
            caption="Ticket #17",
        )

        assert result == 702
        bot.send_video.assert_awaited_once()
        sent = bot.send_video.await_args.kwargs
        assert sent["chat_id"] == -100123
        assert sent["message_thread_id"] == 42
        assert sent["caption"] == "Ticket #17"
        assert sent["supports_streaming"] is True
        assert sent["video"].filename == "record.mp4"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("media_type", "filename", "mime_type"),
        [
            ("document", "client.log", "text/plain"),
            ("document", "report.pdf", "application/pdf"),
            ("voice", "voice.ogg", "audio/ogg"),
            ("audio", "sound.mp3", "audio/mpeg"),
            ("animation", "demo.gif", "image/gif"),
            ("video", "video.avi", "video/x-msvideo"),
            ("unknown_type", "file.bin", None),
        ],
    )
    async def test_documents_and_non_mp4_send_via_send_document(
        self, media_type: str, filename: str, mime_type: str | None
    ) -> None:
        sender, bot = make_sender()
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=703))

        media = TicketMedia(
            media_type=media_type,
            media_url="https://bedolaga/media/test",
            filename=filename,
            mime_type=mime_type,
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
            message_thread_id=42,
        )

        assert result == 703
        bot.send_document.assert_awaited_once()
        sent = bot.send_document.await_args.kwargs
        assert sent["chat_id"] == -100123
        assert sent["message_thread_id"] == 42
        assert sent["document"].filename == filename

    @pytest.mark.asyncio
    async def test_video_falls_back_to_send_document_on_telegram_bad_request(self) -> None:
        sender, bot = make_sender()
        bot.send_video = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="Bad Request: failed to get HTTP URL content"
            )
        )
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=704))

        media = TicketMedia(
            media_type="video",
            media_url="https://bedolaga/media/test.mp4",
            filename="record.mp4",
            mime_type="video/mp4",
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
            message_thread_id=42,
            caption="Ticket #17",
        )

        assert result == 704
        assert bot.send_video.await_count == 1
        assert bot.send_document.await_count == 1
        sent_doc = bot.send_document.await_args.kwargs
        assert sent_doc["chat_id"] == -100123
        assert sent_doc["message_thread_id"] == 42
        assert sent_doc["caption"] == "Ticket #17"
        assert sent_doc["document"].filename == "record.mp4"

    @pytest.mark.asyncio
    async def test_video_does_not_retry_on_network_or_other_exception(self) -> None:
        sender, bot = make_sender()
        bot.send_video = AsyncMock(side_effect=RuntimeError("connection timeout"))
        bot.send_document = AsyncMock()

        media = TicketMedia(
            media_type="video",
            media_url="https://bedolaga/media/test.mp4",
            filename="record.mp4",
            mime_type="video/mp4",
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
        )

        assert result is None
        assert bot.send_video.await_count == 1
        bot.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_known_oversized_file_before_bot_call(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_document = AsyncMock()

        media = TicketMedia(
            media_type="video",
            media_url="https://bedolaga/media/large.mp4",
            filename="large.mp4",
            file_size=50 * 1024 * 1024 + 1,
        )

        result = await sender.send_ticket_media(
            chat_id=-100123,
            media=media,
        )

        assert result is None
        bot.send_photo.assert_not_awaited()
        bot.send_video.assert_not_awaited()
        bot.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_exact_50mb_and_unknown_size(self) -> None:
        sender, bot = make_sender()
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=705))

        media_exact = TicketMedia(
            media_type="document",
            media_url="https://bedolaga/media/exact.bin",
            filename="exact.bin",
            file_size=50 * 1024 * 1024,
        )
        assert await sender.send_ticket_media(100, media_exact) == 705

        media_unknown = TicketMedia(
            media_type="document",
            media_url="https://bedolaga/media/unknown.bin",
            filename="unknown.bin",
            file_size=None,
        )
        assert await sender.send_ticket_media(100, media_unknown) == 705

    @pytest.mark.asyncio
    async def test_returns_none_and_swallows_telegram_exception(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock(side_effect=RuntimeError("telegram is down"))

        media = TicketMedia(
            media_type="photo",
            media_url="https://bedolaga/media/photo.jpg",
            filename="photo.jpg",
        )

        result = await sender.send_ticket_media(100, media)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_message_has_no_message_id(self) -> None:
        sender, bot = make_sender()
        bot.send_photo = AsyncMock(return_value=object())

        media = TicketMedia(
            media_type="photo",
            media_url="https://bedolaga/media/photo.jpg",
            filename="photo.jpg",
        )

        result = await sender.send_ticket_media(100, media)
        assert result is None
