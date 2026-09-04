"""FAQ initialization and sync from JSON file on application startup."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.logging_config import log_failure
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqEntry

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
        # A retained index may belong to another model with the same dimension.
        # Until verified or fully replaced, only its text data is safe to use.
        self.service.set_vector_search_enabled(False)
        if not self.faq_path.exists():
            logger.info("FAQ file is absent; search starts without indexing")
            self.service.mark_ready()
            return

        try:
            await self.service.init_schema()

            current_hash = self.compute_hash(self.faq_path)
            current_fingerprint = (
                self.service.get_index_fingerprint(current_hash)
                if current_hash is not None
                else None
            )
            stored_fingerprint = await self.service.get_faq_index_fingerprint()
            faq_count = await self.service.get_faq_count()
            # Rows without an embedding are invisible to search, so counting rows
            # alone is not evidence that the FAQ is usable.
            indexed_count = await self.service.get_indexed_faq_count()

            if (
                current_fingerprint is not None
                and current_fingerprint == stored_fingerprint
                and faq_count > 0
                and indexed_count == faq_count
            ):
                logger.info(
                    "FAQ database is up to date (index fingerprint matches, %d entries embedded). "
                    "Skipping re-indexing.",
                    indexed_count,
                )
                self.service.set_vector_search_enabled(True)
                self.service.mark_ready()
                return

            if faq_count > 0 and indexed_count < faq_count:
                logger.info(
                    "%d of %d FAQ entries have no embedding; re-indexing",
                    faq_count - indexed_count,
                    faq_count,
                )

            with open(self.faq_path, encoding="utf-8") as f:
                entries: list[dict[str, Any]] = json.load(f)

            logger.info(
                "Indexing %d FAQ entries (stored fingerprint = %s, current fingerprint = %s)",
                len(entries),
                stored_fingerprint,
                current_fingerprint,
            )

            usable = [
                FaqEntry(
                    question=entry["question"],
                    answer=entry["answer"],
                    keywords=self.extract_keywords(entry.get("keywords")),
                    image=self.extract_image(entry.get("image")),
                )
                for entry in entries
                if entry.get("question") and entry.get("answer")
            ]

            indexed = await self.service.replace_faq_batch(usable)

            # The fingerprint is the promise that source content and embedding
            # representation match the active rows.  Storing it after a partial
            # run would keep the next start from retrying the missing vectors.
            if current_fingerprint is not None and indexed == len(usable):
                await self.service.update_faq_index_fingerprint(current_fingerprint)
                self.service.set_vector_search_enabled(True)
            elif indexed != len(usable):
                log_failure(
                    logger,
                    "FAQ indexing incomplete; next start will retry",
                    indexed=indexed,
                    expected=len(usable),
                )

            logger.info("FAQ indexing complete: %d entries indexed", indexed)
            self.service.mark_ready()

        except Exception as e:
            log_failure(logger, "FAQ loading failed; search unavailable", e)

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
            log_failure(logger, "FAQ file hashing failed", e, details={"path": str(file_path)})
            return None

    @staticmethod
    def extract_image(image_value: Any) -> str | None:
        """Normalise the optional image file name on an entry."""
        if isinstance(image_value, str) and image_value.strip():
            return image_value.strip()
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
