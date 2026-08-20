"""Unit tests for PhotoDownloader and MIME detection."""

import base64
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.photo_downloader import PhotoDownloader


class DummyPhotoSize:
    def __init__(self, file_id: str, width: int = 100, height: int = 100):
        self.file_id = file_id
        self.width = width
        self.height = height


class DummyFile:
    def __init__(self, file_id: str, file_path: str):
        self.file_id = file_id
        self.file_path = file_path


@pytest.mark.asyncio
async def test_should_download_and_base64_encode_the_photo():
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=DummyFile("file-1", "photos/img.jpg"))

    async def mock_download_file(file_path: str, destination: io.BytesIO):
        destination.write(b"\x01\x02\x03")

    bot.download_file = AsyncMock(side_effect=mock_download_file)

    downloader = PhotoDownloader(bot)
    photos = [DummyPhotoSize("file-1")]
    result = await downloader.download(photos)

    assert result.is_success()
    assert result.base64_image == base64.b64encode(b"\x01\x02\x03").decode("ascii")
    assert result.mime_type == "image/jpeg"
    assert result.error_message_key is None


@pytest.mark.asyncio
async def test_should_pick_the_largest_size():
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=DummyFile("file-3", "photos/img.png"))

    async def mock_download_file(file_path: str, destination: io.BytesIO):
        destination.write(b"PNG_BYTES")

    bot.download_file = AsyncMock(side_effect=mock_download_file)

    downloader = PhotoDownloader(bot)
    photos = [
        DummyPhotoSize("file-0", 50, 50),
        DummyPhotoSize("file-1", 100, 100),
        DummyPhotoSize("file-2", 200, 200),
        DummyPhotoSize("file-3", 400, 400),
    ]
    result = await downloader.download(photos)

    assert result.is_success()
    bot.get_file.assert_called_once_with("file-3")
    assert result.mime_type == "image/png"


@pytest.mark.asyncio
async def test_should_report_empty_photos():
    bot = MagicMock()
    downloader = PhotoDownloader(bot)

    result1 = await downloader.download([])
    assert not result1.is_success()
    assert result1.error_message_key == "bot.photo.upload.error"

    result2 = await downloader.download(None)
    assert not result2.is_success()
    assert result2.error_message_key == "bot.photo.upload.error"


@pytest.mark.asyncio
async def test_should_report_failed_get_file():
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=None)

    downloader = PhotoDownloader(bot)
    result = await downloader.download([DummyPhotoSize("file-1")])

    assert not result.is_success()
    assert result.error_message_key == "bot.photo.upload.error"


@pytest.mark.asyncio
async def test_should_report_empty_download_body():
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=DummyFile("file-1", "photos/img.jpg"))

    async def mock_empty_download(file_path: str, destination: io.BytesIO):
        pass  # writes nothing

    bot.download_file = AsyncMock(side_effect=mock_empty_download)

    downloader = PhotoDownloader(bot)
    result = await downloader.download([DummyPhotoSize("file-1")])

    assert not result.is_success()
    assert result.error_message_key == "bot.photo.download.error"


@pytest.mark.asyncio
async def test_should_report_unexpected_failure():
    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=Exception("Telegram connection timeout"))

    downloader = PhotoDownloader(bot)
    result = await downloader.download([DummyPhotoSize("file-1")])

    assert not result.is_success()
    assert result.error_message_key == "bot.photo.error"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("photos/img.png", "image/png"),
        ("photos/img.PNG", "image/png"),
        ("photos/img.webp", "image/webp"),
        ("photos/img.gif", "image/gif"),
        ("photos/img.jpg", "image/jpeg"),
        ("photos/img.jpeg", "image/jpeg"),
        ("photos/no-suffix", "image/jpeg"),
        (None, "image/jpeg"),
    ],
)
def test_detect_mime_type(path: str | None, expected: str):
    assert PhotoDownloader.detect_mime_type(path) == expected
