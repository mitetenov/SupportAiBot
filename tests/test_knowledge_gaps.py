"""Unit tests for KnowledgeGapService, trigger classification, cosine deduplication, and gap statistics."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.knowledge_gaps import (
    GapStatsDto,
    KnowledgeGapService,
)
from app.rag.service import FaqContext, FaqResult


class TestGapStatsDto:
    """Test GapStatsDto data model."""

    def test_dto_properties(self) -> None:
        now = datetime.now(UTC)
        dto = GapStatsDto(
            user_query="Как настроить на роутере?",
            gap_count=5,
            trigger_reason="LOW_SIMILARITY",
            first_seen=now,
            last_seen=now,
        )
        assert dto.user_query == "Как настроить на роутере?"
        assert dto.gap_count == 5
        assert dto.trigger_reason == "LOW_SIMILARITY"
        assert dto.first_seen == now
        assert dto.last_seen == now


class TestTriggerClassification:
    """Test determination of trigger reasons."""

    @pytest.fixture
    def service(self) -> KnowledgeGapService:
        return KnowledgeGapService(
            db_manager=MagicMock(),
            faq_service=MagicMock(),
            embedding_provider=MagicMock(),
        )

    def test_determine_trigger_no_match(self, service: KnowledgeGapService) -> None:
        trigger = service.determine_trigger(
            raw_bot_response="Ответ бота",
            max_similarity=0.0,
            best_faq_question=None,
        )
        assert trigger == "NO_MATCH"

    def test_determine_trigger_low_similarity(self, service: KnowledgeGapService) -> None:
        trigger = service.determine_trigger(
            raw_bot_response="Ответ бота",
            max_similarity=0.68,
            best_faq_question="Вопрос 1",
        )
        assert trigger == "LOW_SIMILARITY"

    def test_determine_trigger_escalated(self, service: KnowledgeGapService) -> None:
        trigger = service.determine_trigger(
            raw_bot_response="Вот информация... [ESCALATE]",
            max_similarity=0.85,
            best_faq_question="Вопрос 1",
        )
        assert trigger == "ESCALATED"

    @pytest.mark.parametrize(
        "phrase",
        [
            "К сожалению, я не знаю точного ответа на ваш вопрос.",
            "Я не могу ответить на этот вопрос.",
            "Я не могу помочь с данной проблемой.",
            "Затрудняюсь ответить на вопрос.",
            "Я не обладаю информацией по вашему запросу.",
        ],
    )
    def test_determine_trigger_llm_unsure(self, service: KnowledgeGapService, phrase: str) -> None:
        trigger = service.determine_trigger(
            raw_bot_response=phrase,
            max_similarity=0.82,
            best_faq_question="Вопрос 1",
        )
        assert trigger == "LLM_UNSURE"

    def test_determine_trigger_none_when_high_similarity_and_no_markers(
        self, service: KnowledgeGapService
    ) -> None:
        trigger = service.determine_trigger(
            raw_bot_response="Вот ваша инструкция по настройке Happ...",
            max_similarity=0.89,
            best_faq_question="Как подключить VPN?",
        )
        assert trigger is None


class TestKnowledgeGapServiceEvaluation:
    """Test evaluation and storing of knowledge gaps."""

    @pytest.mark.asyncio
    async def test_evaluate_ignores_empty_query(self) -> None:
        mock_db = MagicMock()
        mock_faq = MagicMock()
        mock_emb = MagicMock()
        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=mock_faq, embedding_provider=mock_emb
        )

        await service.evaluate("", 12345, "response", FaqContext.EMPTY)
        await service.evaluate("   ", 12345, "response", FaqContext.EMPTY)
        mock_faq.embed_query_as_vector.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_skips_when_no_trigger(self) -> None:
        mock_db = MagicMock()
        mock_faq = MagicMock()
        mock_emb = MagicMock()
        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=mock_faq, embedding_provider=mock_emb
        )

        ctx = FaqContext(
            text="FAQ...",
            results=[FaqResult("Вопрос", "Ответ", 0.95, 0.05)],
            max_similarity=0.95,
            best_question="Вопрос",
        )

        await service.evaluate(
            user_query="Как подключиться?",
            telegram_user_id=12345,
            raw_bot_response="Вот инструкция.",
            faq_context=ctx,
        )

        mock_faq.embed_query_as_vector.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_stores_new_gap_when_no_similar_exists(self) -> None:
        mock_db = MagicMock()
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        session.execute = AsyncMock(return_value=mock_result)
        mock_db.session.return_value.__aenter__.return_value = session

        mock_faq = MagicMock()
        mock_faq.embed_query_as_vector = AsyncMock(return_value="[0.1,0.2,0.3]")
        mock_emb = MagicMock()

        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=mock_faq, embedding_provider=mock_emb
        )

        await service.evaluate(
            user_query="Неизвестный вопрос про VPN",
            telegram_user_id=12345,
            raw_bot_response="Ответ",
            faq_context=FaqContext.EMPTY,
        )

        assert session.execute.await_count >= 1
        # Check that insert was executed
        insert_call = session.execute.await_args_list[-1]
        sql_text = str(insert_call.args[0])
        assert "INSERT INTO knowledge_gaps" in sql_text

    @pytest.mark.asyncio
    async def test_evaluate_increments_existing_gap_when_similar_exists(self) -> None:
        mock_db = MagicMock()
        session = MagicMock()
        mock_row = MagicMock()
        mock_row.id = 42
        mock_row.similarity = 0.92

        mock_find_result = MagicMock()
        mock_find_result.fetchone.return_value = mock_row
        session.execute = AsyncMock(return_value=mock_find_result)
        mock_db.session.return_value.__aenter__.return_value = session

        mock_faq = MagicMock()
        mock_faq.embed_query_as_vector = AsyncMock(return_value="[0.1,0.2,0.3]")
        mock_emb = MagicMock()

        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=mock_faq, embedding_provider=mock_emb
        )

        await service.evaluate(
            user_query="Неизвестный вопрос про VPN",
            telegram_user_id=12345,
            raw_bot_response="Ответ",
            faq_context=FaqContext.EMPTY,
        )

        # The last executed query should be the UPDATE query
        update_call = session.execute.await_args_list[-1]
        sql_text = str(update_call.args[0])
        assert "UPDATE knowledge_gaps" in sql_text
        assert "SET gap_count = gap_count + 1" in sql_text
        params = update_call.args[1]
        assert params["id"] == 42

    @pytest.mark.asyncio
    async def test_evaluate_operator_request(self) -> None:
        mock_db = MagicMock()
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        session.execute = AsyncMock(return_value=mock_result)
        mock_db.session.return_value.__aenter__.return_value = session

        mock_faq = MagicMock()
        mock_faq.embed_query_as_vector = AsyncMock(return_value="[0.1,0.2,0.3]")
        mock_emb = MagicMock()

        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=mock_faq, embedding_provider=mock_emb
        )

        await service.evaluate_operator_request(
            user_query="Позовите оператора",
            telegram_user_id=12345,
            faq_context=None,
        )

        insert_call = session.execute.await_args_list[-1]
        params = insert_call.args[1]
        assert params["trigger_reason"] == "USER_OPERATOR"
        assert params["bot_response"] == "[Пользователь запросил оператора после ответа бота]"

    @pytest.mark.asyncio
    async def test_get_top_gaps(self) -> None:
        mock_db = MagicMock()
        session = MagicMock()
        now = datetime.now(UTC)
        mock_row1 = MagicMock()
        mock_row1.user_query = "Вопрос 1"
        mock_row1.gap_count = 10
        mock_row1.trigger_reason = "NO_MATCH"
        mock_row1.first_seen = now
        mock_row1.last_seen = now

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row1]
        session.execute = AsyncMock(return_value=mock_result)
        mock_db.session.return_value.__aenter__.return_value = session

        service = KnowledgeGapService(
            db_manager=mock_db, faq_service=MagicMock(), embedding_provider=MagicMock()
        )
        gaps = await service.get_top_gaps(limit=5)

        assert len(gaps) == 1
        assert gaps[0].user_query == "Вопрос 1"
        assert gaps[0].gap_count == 10
        assert gaps[0].trigger_reason == "NO_MATCH"
