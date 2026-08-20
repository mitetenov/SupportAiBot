"""Unit tests for FaqInitializer and FAQ startup synchronization."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.initializer import FaqInitializer


class TestFaqInitializer:
    """Test suite for FaqInitializer."""

    def test_extract_keywords(self) -> None:
        assert FaqInitializer.extract_keywords(["vpn", "happ", "ios"]) == "vpn, happ, ios"
        assert FaqInitializer.extract_keywords("настройка, роутер") == "настройка, роутер"
        assert FaqInitializer.extract_keywords([]) is None
        assert FaqInitializer.extract_keywords("") is None
        assert FaqInitializer.extract_keywords(None) is None
        assert FaqInitializer.extract_keywords(123) is None

    def test_compute_hash(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_file.write_text('{"test": "data"}', encoding="utf-8")

        initializer = FaqInitializer(service=MagicMock(), faq_path=faq_file)
        hash1 = initializer.compute_hash(faq_file)
        assert hash1 is not None
        assert len(hash1) == 64  # SHA-256 hex length

        # Same content gives same hash
        assert initializer.compute_hash(faq_file) == hash1

        # Changed content gives different hash
        faq_file.write_text('{"test": "modified"}', encoding="utf-8")
        assert initializer.compute_hash(faq_file) != hash1

    @pytest.mark.asyncio
    async def test_run_skips_indexing_when_hash_matches_and_count_positive(
        self, tmp_path: Path
    ) -> None:
        faq_file = tmp_path / "faq.json"
        faq_data = [{"question": "Q1", "answer": "A1", "keywords": ["k1"]}]
        faq_file.write_text(json.dumps(faq_data), encoding="utf-8")

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_hash = AsyncMock()
        mock_service.get_faq_count = AsyncMock(return_value=1)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq = AsyncMock()
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        current_hash = initializer.compute_hash(faq_file)
        mock_service.get_faq_hash.return_value = current_hash

        await initializer.run()

        mock_service.init_schema.assert_awaited_once()
        mock_service.clear_faq.assert_not_called()
        mock_service.index_faq.assert_not_called()
        mock_service.update_faq_hash.assert_not_called()
        mock_service.mark_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_indexes_faq_when_hash_differs_or_empty(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_data = [
            {"question": "Q1", "answer": "A1", "keywords": ["k1", "k2"]},
            {"question": "Q2", "answer": "A2", "keywords": "single_keyword"},
        ]
        faq_file.write_text(json.dumps(faq_data), encoding="utf-8")

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_hash = AsyncMock(return_value="old_different_hash")
        mock_service.get_faq_count = AsyncMock(return_value=0)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq = AsyncMock()
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        await initializer.run()

        mock_service.init_schema.assert_awaited_once()
        mock_service.clear_faq.assert_awaited_once()
        assert mock_service.index_faq.await_count == 2
        mock_service.index_faq.assert_any_await("Q1", "A1", "k1, k2")
        mock_service.index_faq.assert_any_await("Q2", "A2", "single_keyword")
        mock_service.update_faq_hash.assert_awaited_once()
        mock_service.mark_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_handles_missing_file_gracefully(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "non_existent_faq.json"

        mock_service = MagicMock()
        mock_service.mark_ready = MagicMock()

        initializer = FaqInitializer(service=mock_service, faq_path=missing_file)
        await initializer.run()

        mock_service.mark_ready.assert_called_once()
