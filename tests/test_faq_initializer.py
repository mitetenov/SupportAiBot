"""Unit tests for FaqInitializer and FAQ startup synchronization."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.initializer import FaqInitializer


def configure_index_service(service: MagicMock, *, indexed: int) -> None:
    """Configure the index-specific service contract used by the initializer."""
    service.get_index_fingerprint = MagicMock(
        side_effect=lambda source_hash: f"fingerprint:{source_hash}"
    )
    service.get_faq_index_fingerprint = AsyncMock(return_value=None)
    service.replace_faq_batch = AsyncMock(return_value=indexed)
    service.update_faq_index_fingerprint = AsyncMock()


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
        mock_service.get_indexed_faq_count = AsyncMock(return_value=1)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=0)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=0)

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        current_hash = initializer.compute_hash(faq_file)
        mock_service.get_faq_index_fingerprint.return_value = f"fingerprint:{current_hash}"

        await initializer.run()

        mock_service.init_schema.assert_awaited_once()
        mock_service.replace_faq_batch.assert_not_called()
        mock_service.update_faq_index_fingerprint.assert_not_called()
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
        mock_service.get_indexed_faq_count = AsyncMock(return_value=0)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=2)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=2)

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        await initializer.run()

        mock_service.init_schema.assert_awaited_once()
        mock_service.replace_faq_batch.assert_awaited_once()
        indexed = mock_service.replace_faq_batch.await_args.args[0]
        assert [(e.question, e.answer, e.keywords) for e in indexed] == [
            ("Q1", "A1", "k1, k2"),
            ("Q2", "A2", "single_keyword"),
        ]
        mock_service.update_faq_index_fingerprint.assert_awaited_once()
        mock_service.mark_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_handles_missing_file_gracefully(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "non_existent_faq.json"

        mock_service = MagicMock()
        mock_service.mark_ready = MagicMock()

        initializer = FaqInitializer(service=mock_service, faq_path=missing_file)
        await initializer.run()

        mock_service.mark_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_reindexes_when_rows_lost_their_embeddings(self, tmp_path: Path) -> None:
        """A matching hash is not evidence the FAQ is searchable.

        init_schema used to drop and re-add the embedding column on every start.
        With the hash unchanged the initializer then skipped re-indexing, so every
        row sat with a NULL embedding and FAQ search silently returned nothing for
        the entire life of the deployment.
        """
        faq_file = tmp_path / "faq.json"
        faq_data = [
            {"question": "Q1", "answer": "A1", "keywords": ["k1"]},
            {"question": "Q2", "answer": "A2", "keywords": ["k2"]},
        ]
        faq_file.write_text(json.dumps(faq_data), encoding="utf-8")

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_count = AsyncMock(return_value=2)
        mock_service.get_indexed_faq_count = AsyncMock(return_value=0)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=0)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=0)

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        await initializer.run()

        mock_service.replace_faq_batch.assert_awaited_once()
        indexed = mock_service.replace_faq_batch.await_args.args[0]
        assert [e.question for e in indexed] == ["Q1", "Q2"]
        mock_service.mark_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_skips_only_when_every_row_is_embedded(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_file.write_text(json.dumps([{"question": "Q1", "answer": "A1"}]), encoding="utf-8")

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_count = AsyncMock(return_value=10)
        mock_service.get_indexed_faq_count = AsyncMock(return_value=9)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=0)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=0)

        initializer = FaqInitializer(service=mock_service, faq_path=faq_file)
        await initializer.run()

        mock_service.replace_faq_batch.assert_awaited_once()


class TestPartialIndexingKeepsTheHashStale:
    """A run that could not embed everything must not claim the file is indexed."""

    @pytest.mark.asyncio
    async def test_hash_is_not_stored_when_some_entries_failed(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_file.write_text(
            json.dumps([{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}]),
            encoding="utf-8",
        )

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_hash = AsyncMock(return_value="stale")
        mock_service.get_faq_count = AsyncMock(return_value=0)
        mock_service.get_indexed_faq_count = AsyncMock(return_value=0)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=1)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=1)

        await FaqInitializer(service=mock_service, faq_path=faq_file).run()

        mock_service.update_faq_index_fingerprint.assert_not_called()

    @pytest.mark.asyncio
    async def test_hash_is_stored_when_every_entry_landed(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_file.write_text(json.dumps([{"question": "Q1", "answer": "A1"}]), encoding="utf-8")

        mock_service = MagicMock()
        mock_service.init_schema = AsyncMock()
        mock_service.get_faq_hash = AsyncMock(return_value="stale")
        mock_service.get_faq_count = AsyncMock(return_value=0)
        mock_service.get_indexed_faq_count = AsyncMock(return_value=0)
        mock_service.clear_faq = AsyncMock()
        mock_service.index_faq_batch = AsyncMock(return_value=1)
        mock_service.update_faq_hash = AsyncMock()
        mock_service.mark_ready = MagicMock()
        configure_index_service(mock_service, indexed=1)

        await FaqInitializer(service=mock_service, faq_path=faq_file).run()

        mock_service.update_faq_index_fingerprint.assert_awaited_once()


class TestIllustrations:
    """An entry may name a screenshot to send alongside the answer."""

    @pytest.mark.asyncio
    async def test_carries_the_image_name_into_the_indexed_entry(self, tmp_path: Path) -> None:
        faq_file = tmp_path / "faq.json"
        faq_file.write_text(
            json.dumps(
                [
                    {"question": "Где кнопка?", "answer": "Слева", "image": "happ-buttons.png"},
                    {"question": "Сколько устройств?", "answer": "Зависит от тарифа"},
                ]
            ),
            encoding="utf-8",
        )

        service = MagicMock()
        service.init_schema = AsyncMock()
        service.get_faq_hash = AsyncMock(return_value=None)
        service.get_faq_count = AsyncMock(return_value=0)
        service.get_indexed_faq_count = AsyncMock(return_value=0)
        service.clear_faq = AsyncMock()
        service.index_faq_batch = AsyncMock(return_value=2)
        service.update_faq_hash = AsyncMock()
        service.mark_ready = MagicMock()
        configure_index_service(service, indexed=2)

        await FaqInitializer(service=service, faq_path=faq_file).run()

        indexed = service.replace_faq_batch.await_args.args[0]
        assert indexed[0].image == "happ-buttons.png"
        assert indexed[1].image is None
