"""Unit tests for FaqEmbeddingService, PGVector hybrid search, RRF fusion, and FAQ context formatting."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.service import (
    CONNECTION_FAQ_QUERY,
    EMBEDDING_CACHE_SIZE,
    REFERRAL_FAQ_QUERY,
    FaqContext,
    FaqEmbeddingService,
    FaqResult,
)


class DummyEmbeddingProvider:
    """Mock embedding provider for unit tests."""

    def __init__(self, dimension: int = 4, default_val: float = 0.5) -> None:
        self.dimension = dimension
        self.default_val = default_val
        self.embed_mock = AsyncMock(side_effect=self._mock_embed)
        self.call_count = 0

    def get_dimension(self) -> int:
        return self.dimension

    async def embed(self, text: str) -> list[float]:
        self.call_count += 1
        return await self.embed_mock(text)

    def _mock_embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        return [self.default_val] * self.dimension


class TestFaqDataStructures:
    """Test FaqResult and FaqContext data models."""

    def test_faq_result_properties(self) -> None:
        res = FaqResult(
            question="Как оплатить?",
            answer="В боте @PeipivoSalesBot",
            similarity=0.88,
            rrf_score=0.032,
        )
        assert res.question == "Как оплатить?"
        assert res.answer == "В боте @PeipivoSalesBot"
        assert res.similarity == 0.88
        assert res.rrf_score == 0.032

    def test_faq_context_empty_sentinel(self) -> None:
        empty = FaqContext.EMPTY
        assert empty.is_empty() is True
        assert empty.text == ""
        assert empty.results == []
        assert empty.max_similarity == 0.0
        assert empty.best_question is None
        assert empty.questions() == set()

    def test_faq_context_with_results(self) -> None:
        r1 = FaqResult("В1", "О1", 0.9, 0.05)
        r2 = FaqResult("В2", "О2", 0.8, 0.04)
        ctx = FaqContext(
            text="FAQ (...):\nВ1\nО1\n\nВ2\nО2\n\n",
            results=[r1, r2],
            max_similarity=0.9,
            best_question="В1",
        )
        assert ctx.is_empty() is False
        assert len(ctx.results) == 2
        assert ctx.max_similarity == 0.9
        assert ctx.best_question == "В1"
        assert ctx.questions() == {"В1", "В2"}


class TestFaqEmbeddingServiceCache:
    """Test LRU caching behavior for query embeddings."""

    @pytest.mark.asyncio
    async def test_embedding_cache_avoids_repeated_provider_calls(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)

        vec1 = await service.embed_query("Как подключить?")
        vec2 = await service.embed_query("Как подключить?")

        assert vec1 == [0.5, 0.5, 0.5, 0.5]
        assert vec2 == vec1
        assert provider.call_count == 1

    @pytest.mark.asyncio
    async def test_embedding_cache_evicts_oldest_when_full(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)

        # Fill cache beyond limit
        for i in range(EMBEDDING_CACHE_SIZE + 10):
            await service.embed_query(f"Query {i}")

        assert len(service.embedding_cache) <= EMBEDDING_CACHE_SIZE

    @pytest.mark.asyncio
    async def test_embed_query_empty_returns_none(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)

        assert await service.embed_query("") is None
        assert await service.embed_query("   ") is None
        assert provider.call_count == 0


class TestFaqEmbeddingServiceAliasesAndFormatting:
    """Test keywords formatting and global aliases."""

    def test_with_global_aliases_with_keywords(self) -> None:
        service = FaqEmbeddingService(
            db_manager=MagicMock(), embedding_provider=DummyEmbeddingProvider()
        )
        result = service.with_global_aliases("настройка, роутер")
        assert "настройка, роутер, vpn, впн, вэпэн" == result

    def test_with_global_aliases_empty_keywords(self) -> None:
        service = FaqEmbeddingService(
            db_manager=MagicMock(), embedding_provider=DummyEmbeddingProvider()
        )
        result = service.with_global_aliases(None)
        assert "vpn, впн, вэпэн" == result
        result_blank = service.with_global_aliases("   ")
        assert "vpn, впн, вэпэн" == result_blank

    def test_vector_to_string(self) -> None:
        service = FaqEmbeddingService(
            db_manager=MagicMock(), embedding_provider=DummyEmbeddingProvider()
        )
        vec_str = service.vector_to_string([0.1, -0.2, 0.35])
        assert vec_str == "[0.1,-0.2,0.35]"


class TestFaqSearchLogic:
    """Test search, hybrid SQL execution, RRF ranking, and fallback logic."""

    @pytest.mark.asyncio
    async def test_search_not_ready_returns_empty(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        assert service.is_ready() is False

        results = await service.search("Как настроить VPN?")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_when_ready_calls_db(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        session = MagicMock()
        mock_result = MagicMock()
        session.execute = AsyncMock(return_value=mock_result)
        db_manager.session.return_value.__aenter__.return_value = session

        # Mock DB execute returning rows for hybrid search
        mock_row1 = MagicMock()
        mock_row1.question = "Как настроить VPN?"
        mock_row1.answer = "Инструкция..."
        mock_row1.vector_sim = 0.89
        mock_row1.fts_rank = 0.15
        mock_row1.rrf_score = 0.032

        mock_result.fetchall.return_value = [mock_row1]

        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()

        results = await service.search("Как настроить VPN?")
        assert len(results) == 1
        assert results[0].question == "Как настроить VPN?"
        assert results[0].similarity == 0.89
        assert results[0].rrf_score == 0.032

    @pytest.mark.asyncio
    async def test_search_falls_back_to_vector_on_hybrid_error(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        session = MagicMock()
        db_manager.session.return_value.__aenter__.return_value = session

        # First call (hybrid search) raises Exception, second call (vector search) succeeds
        mock_row = MagicMock()
        mock_row.question = "Как настроить VPN?"
        mock_row.answer = "Инструкция..."
        mock_row.vector_sim = 0.75

        mock_vector_result = MagicMock()
        mock_vector_result.fetchall.return_value = [mock_row]

        session.execute = AsyncMock(side_effect=[Exception("FTS error"), mock_vector_result])

        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()

        results = await service.search("Как настроить VPN?")
        assert len(results) == 1
        assert results[0].question == "Как настроить VPN?"
        assert results[0].similarity == 0.75

    @pytest.mark.asyncio
    async def test_search_with_fallback_connection_issues(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()

        # Mock search method
        async def mock_search_fn(q: str, exclude: set[str] | None = None) -> list[FaqResult]:
            if q == CONNECTION_FAQ_QUERY:
                return [
                    FaqResult("Не могу подключиться к VPN", "Инструкция 1", 0.85, 0.03),
                    FaqResult("Не работает VPN", "Отключитесь и обновите", 0.70, 0.02),
                ]
            elif "не работает" in q.lower():
                return [FaqResult("Не работает VPN", "Отключитесь и обновите", 0.70, 0.02)]
            return []

        service.search = AsyncMock(side_effect=mock_search_fn)  # type: ignore[method-assign]

        results = await service.search_with_fallback("У меня не работает впн")
        assert len(results) == 2
        # Deduplicated and sorted by rrf_score desc
        assert results[0].question == "Не могу подключиться к VPN"
        assert results[1].question == "Не работает VPN"

    @pytest.mark.asyncio
    async def test_search_with_fallback_referral_query(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()

        async def mock_search_fn(q: str, exclude: set[str] | None = None) -> list[FaqResult]:
            if q == REFERRAL_FAQ_QUERY:
                return [
                    FaqResult("Реферальная программа", "Ответ реф", 0.80, 0.03),
                    FaqResult("Партнерка", "Ответ", 0.68, 0.02),
                ]
            elif "партнер" in q.lower():
                return [FaqResult("Партнерка", "Ответ", 0.68, 0.02)]
            return []

        service.search = AsyncMock(side_effect=mock_search_fn)  # type: ignore[method-assign]

        results = await service.search_with_fallback("Где моя партнерская ссылка?")
        assert len(results) == 2
        assert results[0].question == "Реферальная программа"
        assert results[1].question == "Партнерка"

    @pytest.mark.asyncio
    async def test_build_faq_context_formats_and_filters_excluded(self) -> None:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()

        r1 = FaqResult("Вопрос 1", "Инструкция 1", 0.88, 0.035)
        r2 = FaqResult("Вопрос 2", "Инструкция 2", 0.75, 0.025)

        service.search_with_fallback = AsyncMock(return_value=[r1, r2])  # type: ignore[method-assign]

        # 1. Without exclusions
        ctx = await service.build_faq_context("Как подключить?")
        assert ctx.is_empty() is False
        assert ctx.max_similarity == 0.88
        assert ctx.best_question == "Вопрос 1"
        assert "FAQ (скопируй инструкцию дословно в ответ, не добавляй своих шагов):\n" in ctx.text
        assert "Вопрос: Вопрос 1\nИнструкция: Инструкция 1\n\n" in ctx.text
        assert "Вопрос: Вопрос 2\nИнструкция: Инструкция 2\n\n" in ctx.text

        # 2. With exclusion of Вопрос 1
        ctx2 = await service.build_faq_context("Как подключить?", exclude_questions={"Вопрос 1"})
        assert ctx2.is_empty() is False
        assert len(ctx2.results) == 1
        assert ctx2.best_question == "Вопрос 2"
        assert ctx2.max_similarity == 0.75
        assert "Вопрос 1" not in ctx2.text
        assert "Вопрос: Вопрос 2\nИнструкция: Инструкция 2\n\n" in ctx2.text

        # 3. With all excluded
        ctx3 = await service.build_faq_context(
            "Как подключить?", exclude_questions={"Вопрос 1", "Вопрос 2"}
        )
        assert ctx3.is_empty() is True
        assert ctx3 == FaqContext.EMPTY


class TestExclusionHappensInTheQuery:
    """Already-shown entries must be excluded by the query, not after it.

    The system prompt promises the model that "уже показанные инструкции
    исключаются из подборки автоматически". Filtering the result set afterwards
    could not honour that: the SQL LIMIT had already spent its three slots on
    entries the user had just rejected, so a follow-up like «все равно не
    работает» produced an empty FAQ context and the bot fell back to asking
    which server the user was on — instead of moving on to the next instruction.
    """

    @staticmethod
    def _service_with_capture() -> tuple[FaqEmbeddingService, list[dict]]:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        session = MagicMock()
        captured: list[dict] = []

        async def execute(_stmt, params=None):
            captured.append(params or {})
            result = MagicMock()
            result.fetchall.return_value = []
            return result

        session.execute = AsyncMock(side_effect=execute)
        db_manager.session.return_value.__aenter__.return_value = session

        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        service.mark_ready()
        return service, captured

    @pytest.mark.asyncio
    async def test_excluded_questions_reach_the_sql_parameters(self) -> None:
        service, captured = self._service_with_capture()

        await service.search("не работает", exclude={"Вопрос Б", "Вопрос А"})

        assert captured, "query was never executed"
        assert captured[0]["excluded"] == ["Вопрос А", "Вопрос Б"]

    @pytest.mark.asyncio
    async def test_no_exclusions_sends_an_empty_array(self) -> None:
        service, captured = self._service_with_capture()

        await service.search("не работает")

        assert captured[0]["excluded"] == []

    @pytest.mark.asyncio
    async def test_fallback_searches_exclude_the_same_entries(self) -> None:
        service, captured = self._service_with_capture()

        # "не работает" trips the connection fallback, so more than one search runs.
        await service.search_with_fallback("не работает впн", exclude={"Показанный вопрос"})

        assert len(captured) >= 2, "connection fallback did not run"
        for params in captured:
            assert params["excluded"] == ["Показанный вопрос"]
