"""FAQ initialization and sync from JSON file on application startup."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.rag.service import FaqEmbeddingService

logger = logging.getLogger(__name__)

DEFAULT_FAQ_PATH = Path("faq/faq.json")


class FaqInitializer:
    """Synchronizes FAQ entries from local JSON configuration into the vector database."""

    def __init__(
        self,
        service: FaqEmbeddingService,
        faq_path: str | Path = DEFAULT_FAQ_PATH,
    ) -> None:
        self.service = service
        self.faq_path = Path(faq_path)

    async def run(self) -> None:
        """Run startup sync check and re-index if FAQ file changed."""
        if not self.faq_path.exists():
            logger.warning("FAQ file not found at path: %s", self.faq_path)
            self.service.mark_ready()
            return

        try:
            await self.service.init_schema()

            current_hash = self.compute_hash(self.faq_path)
            stored_hash = await self.service.get_faq_hash()
            faq_count = await self.service.get_faq_count()
            # Rows without an embedding are invisible to search, so counting rows
            # alone is not evidence that the FAQ is usable.
            indexed_count = await self.service.get_indexed_faq_count()

            if (
                current_hash is not None
                and current_hash == stored_hash
                and faq_count > 0
                and indexed_count == faq_count
            ):
                logger.info(
                    "FAQ database is up to date (hash matches, %d entries embedded). "
                    "Skipping re-indexing.",
                    indexed_count,
                )
                self.service.mark_ready()
                return

            if faq_count > 0 and indexed_count < faq_count:
                logger.warning(
                    "%d of %d FAQ entries have no embedding — re-indexing regardless of the hash",
                    faq_count - indexed_count,
                    faq_count,
                )

            with open(self.faq_path, encoding="utf-8") as f:
                entries: list[dict[str, Any]] = json.load(f)

            logger.info(
                "Indexing %d FAQ entries (stored hash = %s, current hash = %s)",
                len(entries),
                stored_hash,
                current_hash,
            )

            await self.service.clear_faq()

            for entry in entries:
                question = entry.get("question")
                answer = entry.get("answer")
                keywords = self.extract_keywords(entry.get("keywords"))
                if question and answer:
                    await self.service.index_faq(question, answer, keywords)

            if current_hash is not None:
                await self.service.update_faq_hash(current_hash)

            logger.info("FAQ indexing complete: %d entries indexed", len(entries))
            self.service.mark_ready()

        except Exception as e:
            logger.error("Failed to load FAQ file — FAQ search will be unavailable: %s", e)

    @staticmethod
    def compute_hash(file_path: Path) -> str | None:
        """Compute SHA-256 hex digest of the given file."""
        try:
            hasher = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(4096):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            logger.error("Failed to compute FAQ file hash for %s: %s", file_path, e)
            return None

    @staticmethod
    def extract_keywords(keywords_value: Any) -> str | None:
        """Extract and format keywords from JSON structure (list or string)."""
        if isinstance(keywords_value, list) and keywords_value:
            filtered = [str(x).strip() for x in keywords_value if str(x).strip()]
            return ", ".join(filtered) if filtered else None
        elif isinstance(keywords_value, str) and keywords_value.strip():
            return keywords_value.strip()
        return None
