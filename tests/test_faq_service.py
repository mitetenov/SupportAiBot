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
from app.rag.types import FaqEntry


class DummyEmbeddingProvider:
    """Mock embedding provider for unit tests."""

    def __init__(self, dimension: int = 4, default_val: float = 0.5) -> None:
        self.dimension = dimension
        self.default_val = default_val
        self.embed_mock = AsyncMock(side_effect=self._mock_embed)
        self.call_count = 0
        self.batch_calls: list[list[str]] = []

    def get_dimension(self) -> int:
        return self.dimension

    async def embed(self, text: str) -> list[float]:
        self.call_count += 1
        return await self.embed_mock(text)

    def _mock_embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        return [self.default_val] * self.dimension

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [await self.embed(item) for item in texts]


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
        # The fallback widens the context without displacing the primary hit.
        assert results[0].question == "Не работает VPN"
        assert results[1].question == "Не могу подключиться к VPN"

    @pytest.mark.asyncio
    async def test_a_fallback_hit_never_outranks_what_the_user_asked_about(self) -> None:
        """The canned topic query matches its own FAQ entry almost exactly.

        Scored against the user's real question it wins on RRF nearly every
        time, and the keyword list that triggers it contains "подписк" — so a
        billing question was answered with connection troubleshooting at rank 1.
        """
        provider = DummyEmbeddingProvider(dimension=4)
        service = FaqEmbeddingService(db_manager=MagicMock(), embedding_provider=provider)
        service.mark_ready()

        async def mock_search_fn(q: str, exclude: set[str] | None = None) -> list[FaqResult]:
            if q == CONNECTION_FAQ_QUERY:
                return [FaqResult("Не могу подключиться к VPN", "Инструкция", 0.88, 0.03)]
            return [FaqResult("Как отменить подписку", "Управление подпиской", 0.61, 0.02)]

        service.search = AsyncMock(side_effect=mock_search_fn)  # type: ignore[method-assign]

        results = await service.search_with_fallback("хочу отменить подписку")

        assert [r.question for r in results] == [
            "Как отменить подписку",
            "Не могу подключиться к VPN",
        ]

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
        assert results[0].question == "Партнерка"
        assert results[1].question == "Реферальная программа"

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


class TestBatchIndexing:
    """Startup indexing must not cost one round trip per FAQ entry."""

    @staticmethod
    def _service() -> tuple[FaqEmbeddingService, DummyEmbeddingProvider, AsyncMock]:
        provider = DummyEmbeddingProvider(dimension=4)
        db_manager = MagicMock()
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        db_manager.session.return_value.__aenter__.return_value = session
        service = FaqEmbeddingService(db_manager=db_manager, embedding_provider=provider)
        return service, provider, session.execute

    @pytest.mark.asyncio
    async def test_one_embedding_call_and_one_insert_for_the_whole_file(self) -> None:
        service, provider, execute = self._service()
        entries = [FaqEntry(f"Q{i}", f"A{i}", f"k{i}") for i in range(5)]

        indexed = await service.index_faq_batch(entries)

        assert indexed == 5
        assert len(provider.batch_calls) == 1, "entries were embedded one at a time"
        assert len(provider.batch_calls[0]) == 5
        assert execute.await_count == 1, "rows were inserted in separate transactions"
        rows = execute.await_args.args[1]
        assert len(rows) == 5
        assert [row["question"] for row in rows] == ["Q0", "Q1", "Q2", "Q3", "Q4"]

    @pytest.mark.asyncio
    async def test_splits_into_chunks_beyond_the_batch_size(self, monkeypatch) -> None:
        monkeypatch.setattr("app.rag.service.EMBED_BATCH_SIZE", 2)
        service, provider, execute = self._service()

        indexed = await service.index_faq_batch([FaqEntry(f"Q{i}", f"A{i}") for i in range(5)])

        assert indexed == 5
        assert [len(call) for call in provider.batch_calls] == [2, 2, 1]
        assert execute.await_count == 1, "one INSERT should still cover every chunk"

    @pytest.mark.asyncio
    async def test_skips_entries_the_provider_could_not_embed(self) -> None:
        service, provider, execute = self._service()

        async def embed_batch(texts: list[str]) -> list[list[float]]:
            return [[] if "Q1" in item else [0.5] * 4 for item in texts]

        provider.embed_batch = embed_batch  # type: ignore[method-assign]

        indexed = await service.index_faq_batch(
            [FaqEntry("Q0", "A0"), FaqEntry("Q1", "A1"), FaqEntry("Q2", "A2")]
        )

        assert indexed == 2
        assert [row["question"] for row in execute.await_args.args[1]] == ["Q0", "Q2"]

    @pytest.mark.asyncio
    async def test_writes_nothing_when_the_provider_is_down(self) -> None:
        service, provider, execute = self._service()

        async def embed_batch(texts: list[str]) -> list[list[float]]:
            return [[] for _ in texts]

        provider.embed_batch = embed_batch  # type: ignore[method-assign]

        assert await service.index_faq_batch([FaqEntry("Q0", "A0")]) == 0
        execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_input_touches_nothing(self) -> None:
        service, provider, execute = self._service()

        assert await service.index_faq_batch([]) == 0
        assert provider.batch_calls == []
        execute.assert_not_called()


class TestEmbedMany:
    """Batch embedding has to respect the cache the single-text path fills."""

    @staticmethod
    def _service() -> tuple[FaqEmbeddingService, DummyEmbeddingProvider]:
        provider = DummyEmbeddingProvider(dimension=4)
        service = FaqEmbeddingService(db_manager=MagicMock(), embedding_provider=provider)
        return service, provider

    @pytest.mark.asyncio
    async def test_asks_the_provider_only_for_uncached_texts(self) -> None:
        service, provider = self._service()
        await service.embed("уже в кэше")

        vectors = await service.embed_many(["уже в кэше", "новый"])

        assert provider.batch_calls == [["новый"]]
        assert vectors[0] == [0.5] * 4
        assert vectors[1] == [0.5] * 4

    @pytest.mark.asyncio
    async def test_keeps_the_result_aligned_with_the_input(self) -> None:
        service, provider = self._service()

        vectors = await service.embed_many(["первый", "   ", "третий"])

        assert len(vectors) == 3
        assert vectors[1] == [], "a blank input must not consume a provider slot"
        assert provider.batch_calls == [["первый", "третий"]]

    @pytest.mark.asyncio
    async def test_caches_what_it_fetched(self) -> None:
        service, provider = self._service()

        await service.embed_many(["первый"])
        await service.embed_many(["первый"])

        assert provider.batch_calls == [["первый"]]

    @pytest.mark.asyncio
    async def test_stays_within_the_cache_bound(self) -> None:
        service, _ = self._service()

        await service.embed_many([f"текст {i}" for i in range(EMBEDDING_CACHE_SIZE + 10)])

        assert len(service.embedding_cache) == EMBEDDING_CACHE_SIZE


class TestFallbacksRunConcurrently:
    """The fallback lookups are independent, so they must not queue up."""

    @pytest.mark.asyncio
    async def test_fallback_search_is_not_awaited_one_after_another(self) -> None:
        import asyncio

        provider = DummyEmbeddingProvider(dimension=4)
        service = FaqEmbeddingService(db_manager=MagicMock(), embedding_provider=provider)
        service.mark_ready()

        in_flight = 0
        peak = 0

        async def slow_search(query: str, exclude=None):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return []

        service.search = slow_search  # type: ignore[method-assign]

        # Trips both the connection and the referral fallback.
        await service.search_with_fallback("впн не работает, где реферальная ссылка")

        assert peak == 3, f"searches ran with only {peak} in flight at once"
