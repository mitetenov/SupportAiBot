"""PDF download validation at the Telegram boundary."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.photo_downloader import MAX_PDF_BYTES, PhotoDownloader


def document(**changes):
    values = {
        "file_id": "pdf-1",
        "file_name": "receipt.PDF",
        "mime_type": "application/pdf",
        "file_size": 30,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def bot_with_content(content):
    bot = MagicMock()
    bot.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="documents/receipt.pdf", file_size=None)
    )

    async def download(_path, destination):
        destination.write(content)

    bot.download_file = AsyncMock(side_effect=download)
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["application/pdf", "application/octet-stream", None])
async def test_pdf_download_preserves_exact_bytes(mime):
    content = b"%PDF-1.4\nreceipt\n%%EOF"
    bot = bot_with_content(content)
    result = await PhotoDownloader(bot).download_pdf(document(mime_type=mime))
    assert result.is_success()
    assert base64.b64decode(result.base64_pdf) == content
    bot.get_file.assert_awaited_once_with("pdf-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,error",
    [
        ({"file_name": "receipt.txt", "mime_type": "text/plain"}, "bot.pdf.invalid"),
        ({"file_id": None}, "bot.pdf.error"),
        ({"file_size": MAX_PDF_BYTES + 1}, "bot.pdf.too_large"),
    ],
)
async def test_rejects_invalid_metadata_before_network(changes, error):
    bot = bot_with_content(b"%PDF-1.4")
    result = await PhotoDownloader(bot).download_pdf(document(**changes))
    assert result.error_message_key == error
    bot.get_file.assert_not_awaited()
    bot.download_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,error", [(b"", "bot.pdf.error"), (b"<html>not PDF</html>", "bot.pdf.invalid")]
)
async def test_rejects_empty_or_disguised_pdf(content, error):
    result = await PhotoDownloader(bot_with_content(content)).download_pdf(document())
    assert not result.is_success()
    assert result.error_message_key == error


@pytest.mark.asyncio
async def test_bounds_download_when_sizes_are_missing_or_forged(monkeypatch):
    monkeypatch.setattr("app.bot.photo_downloader.MAX_PDF_BYTES", 12)
    bot = bot_with_content(b"%PDF-" + b"x" * 8)
    result = await PhotoDownloader(bot).download_pdf(document(file_size=1))
    assert result.error_message_key == "bot.pdf.too_large"


@pytest.mark.asyncio
async def test_rejects_oversized_get_file_response_before_download():
    bot = bot_with_content(b"%PDF-1.4")
    bot.get_file.return_value.file_size = MAX_PDF_BYTES + 1
    result = await PhotoDownloader(bot).download_pdf(document(file_size=None))
    assert result.error_message_key == "bot.pdf.too_large"
    bot.download_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_info", [None, SimpleNamespace(file_path=None)])
async def test_missing_telegram_file_path(file_info):
    bot = bot_with_content(b"%PDF-1.4")
    bot.get_file.return_value = file_info
    result = await PhotoDownloader(bot).download_pdf(document())
    assert result.error_message_key == "bot.pdf.error"
    bot.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_failure_is_user_visible():
    bot = bot_with_content(b"%PDF-1.4")
    bot.download_file.side_effect = RuntimeError("download failed")
    result = await PhotoDownloader(bot).download_pdf(document())
    assert result.error_message_key == "bot.pdf.error"
