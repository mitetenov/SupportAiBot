"""Downloads a Telegram photo and encodes it for multimodal vision APIs."""

import base64
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)

DEFAULT_MIME_TYPE = "image/jpeg"


@dataclass(frozen=True)
class PhotoDownloadResult:
    """Result of attempting to download a Telegram photo."""

    base64_image: str | None = None
    mime_type: str | None = None
    error_message_key: str | None = None

    @classmethod
    def ok(cls, base64_image: str, mime_type: str) -> "PhotoDownloadResult":
        """Factory for successful download."""
        return cls(base64_image=base64_image, mime_type=mime_type, error_message_key=None)

    @classmethod
    def failed(cls, error_message_key: str) -> "PhotoDownloadResult":
        """Factory for failed download carrying user-facing error key."""
        return cls(base64_image=None, mime_type=None, error_message_key=error_message_key)

    def is_success(self) -> bool:
        """Return True if image was successfully downloaded."""
        return self.base64_image is not None


class PhotoDownloader:
    """Downloads Telegram photos and base64-encodes them for vision LLM providers."""

    Result = PhotoDownloadResult

    def __init__(self, bot: Bot, http_client: Any = None) -> None:
        self.bot = bot
        self.http_client = http_client

    @staticmethod
    def detect_mime_type(file_path: str | None) -> str:
        """Derive the MIME type from the Telegram file path suffix."""
        if not file_path:
            return DEFAULT_MIME_TYPE
        lower = file_path.lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".webp"):
            return "image/webp"
        if lower.endswith(".gif"):
            return "image/gif"
        return DEFAULT_MIME_TYPE

    async def download(self, photos: Sequence[Any] | None) -> PhotoDownloadResult:
        """Download the highest-resolution photo from the provided photo sizes."""
        if not photos:
            return PhotoDownloadResult.failed("bot.photo.upload.error")

        try:
            largest = photos[-1]
            file_id = getattr(largest, "file_id", None)
            if not file_id:
                return PhotoDownloadResult.failed("bot.photo.upload.error")

            file_info = await self.bot.get_file(file_id)
            if file_info is None or not getattr(file_info, "file_path", None):
                logger.warning("Telegram get_file returned no file path for %s", file_id)
                return PhotoDownloadResult.failed("bot.photo.upload.error")

            file_path = file_info.file_path
            buffer = io.BytesIO()

            if hasattr(self.bot, "download_file"):
                await self.bot.download_file(file_path, destination=buffer)
                image_bytes = buffer.getvalue()
            elif self.http_client is not None:
                # Custom HTTP client fallback
                file_url = f"https://api.telegram.org/file/bot{self.bot.token}/{file_path}"
                resp = await self.http_client.get(file_url)
                image_bytes = resp.content
            else:
                return PhotoDownloadResult.failed("bot.photo.download.error")

            if not image_bytes:
                return PhotoDownloadResult.failed("bot.photo.download.error")

            b64_str = base64.b64encode(image_bytes).decode("ascii")
            mime = self.detect_mime_type(file_path)
            return PhotoDownloadResult.ok(b64_str, mime)
        except Exception as e:
            logger.error("Error downloading photo from Telegram: %s", e, exc_info=True)
            return PhotoDownloadResult.failed("bot.photo.error")
