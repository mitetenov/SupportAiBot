"""Knowledge gap service for capturing unanswered queries, low confidence, and escalations."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.rag.embedding import EmbeddingProvider
from app.rag.service import FaqContext, FaqEmbeddingService
from app.storage.database import DatabaseSessionManager
from app.storage.schema import ensure_knowledge_gap_schema

logger = logging.getLogger(__name__)

DEDUP_SIMILARITY_THRESHOLD: float = 0.85
DEFAULT_TOP_LIMIT: int = 15
MAX_QUERY_LENGTH: int = 2000
MAX_RESPONSE_LENGTH: int = 500

INSERT_GAP_SQL = text("""
    INSERT INTO knowledge_gaps (
        user_query, embedding, telegram_id, best_faq_question,
        max_similarity, faq_count, trigger_reason, bot_response,
        gap_count, first_seen, last_seen
    )
    VALUES (
        :user_query, CAST(:vector_str AS vector), :telegram_id, :best_faq_question,
        :max_similarity, :faq_count, :trigger_reason, :bot_response,
        1, :first_seen, :last_seen
    )
""")

FIND_SIMILAR_GAP_SQL = text("""
    SELECT id, 1 - (embedding <=> CAST(:vector_str AS vector)) AS similarity
    FROM knowledge_gaps
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:vector_str AS vector)
    LIMIT 1
""")

#: Fallback lookup for gaps stored without an embedding. Cosine dedup only sees
#: rows where the vector is present, so without this an embedding outage turns
#: every repeat of the same question into another row of one.
FIND_GAP_BY_QUERY_SQL = text("""
    SELECT id
    FROM knowledge_gaps
    WHERE user_query = :user_query
    ORDER BY id
    LIMIT 1
""")

UPDATE_GAP_COUNT_SQL = text("""
    UPDATE knowledge_gaps
    SET gap_count = gap_count + 1, last_seen = :last_seen
    WHERE id = :id
""")

SELECT_TOP_GAPS_SQL = text("""
    SELECT user_query, gap_count, trigger_reason, first_seen, last_seen
    FROM knowledge_gaps
    ORDER BY gap_count DESC, last_seen DESC
    LIMIT :limit
""")


@dataclass(frozen=True)
class GapStatsDto:
    """Aggregated statistics for a detected knowledge gap."""

    user_query: str
    gap_count: int
    trigger_reason: str | None
    first_seen: datetime
    last_seen: datetime


class KnowledgeGapService:
    """Service for evaluating and recording unanswered user queries to improve FAQ."""

    def __init__(
        self,
        db_manager: DatabaseSessionManager,
        faq_service: FaqEmbeddingService,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.db_manager = db_manager
        self.faq_service = faq_service
        self.embedding_provider = embedding_provider

    async def init_schema(self) -> None:
        """Settle the vector dimension and index for ``knowledge_gaps``.

        The table itself is an ORM model created by ``create_all``.
        """
        logger.info("Initializing knowledge_gaps database schema")
        async with self.db_manager.session() as session:
            await ensure_knowledge_gap_schema(session, self.embedding_provider.get_dimension())
        logger.info("Knowledge gaps schema initialized successfully")

    async def evaluate(
        self,
        user_query: str,
        telegram_user_id: int,
        raw_bot_response: str | None,
        faq_context: FaqContext | None,
    ) -> None:
        """Evaluate a conversation turn and store knowledge gap if retrieval was deficient."""
        try:
            if not user_query or not user_query.strip():
                return

            query = self._truncate(user_query.strip(), MAX_QUERY_LENGTH)
            response = self._truncate_optional(raw_bot_response, MAX_RESPONSE_LENGTH)
            context = faq_context if faq_context is not None else FaqContext.EMPTY

            trigger = self.determine_trigger(
                raw_bot_response,
                context.max_similarity,
                context.best_question,
            )
            if not trigger:
                return

            await self._store_gap(
                user_query=query,
                telegram_user_id=telegram_user_id,
                best_faq_question=context.best_question,
                max_similarity=context.max_similarity,
                faq_count=len(context.results),
                trigger_reason=trigger,
                bot_response=response,
            )
        except Exception as e:
            logger.warning("Failed to evaluate knowledge gap: %s", e)

    async def evaluate_operator_request(
        self,
        user_query: str,
        telegram_user_id: int,
        faq_context: FaqContext | None,
    ) -> None:
        """Record a knowledge gap when user requests a human operator right after bot response."""
        try:
            if not user_query or not user_query.strip():
                return

            query = self._truncate(user_query.strip(), MAX_QUERY_LENGTH)
            bot_response = "[Пользователь запросил оператора после ответа бота]"
            context = faq_context if faq_context is not None else FaqContext.EMPTY

            await self._store_gap(
                user_query=query,
                telegram_user_id=telegram_user_id,
                best_faq_question=context.best_question,
                max_similarity=context.max_similarity,
                faq_count=len(context.results),
                trigger_reason="USER_OPERATOR",
                bot_response=bot_response,
            )
        except Exception as e:
            logger.warning("Failed to evaluate operator knowledge gap: %s", e)

    def determine_trigger(
        self,
        raw_bot_response: str | None,
        max_similarity: float,
        best_faq_question: str | None,
    ) -> str | None:
        """Classify trigger condition: NO_MATCH, LOW_SIMILARITY, ESCALATED, or LLM_UNSURE."""
        if best_faq_question is None and max_similarity == 0.0:
            return "NO_MATCH"

        if 0.0 < max_similarity < 0.72:
            return "LOW_SIMILARITY"

        if raw_bot_response and "[ESCALATE]" in raw_bot_response:
            return "ESCALATED"

        if self._is_llm_unsure(raw_bot_response):
            return "LLM_UNSURE"

        return None

    @staticmethod
    def _is_llm_unsure(response: str | None) -> bool:
        """Check if bot response expresses uncertainty."""
        if not response:
            return False
        lower = response.lower()
        phrases = (
            "не знаю",
            "не могу ответить",
            "не могу помочь",
            "затрудняюсь ответить",
            "не обладаю информацией",
        )
        return any(phrase in lower for phrase in phrases)

    async def _store_gap(
        self,
        user_query: str,
        telegram_user_id: int,
        best_faq_question: str | None,
        max_similarity: float,
        faq_count: int,
        trigger_reason: str,
        bot_response: str | None,
    ) -> None:
        """Insert or increment count for an existing similar knowledge gap."""
        vector_str = await self.faq_service.embed_query_as_vector(user_query)

        if vector_str:
            existing_id = await self._find_similar_gap(vector_str)
        else:
            existing_id = await self._find_gap_by_query(user_query)

        if existing_id is not None:
            await self._increment_gap(existing_id)
            return

        await self._insert_gap(
            user_query=user_query,
            vector_str=vector_str or None,
            telegram_user_id=telegram_user_id,
            best_faq_question=best_faq_question,
            max_similarity=max_similarity,
            faq_count=faq_count,
            trigger_reason=trigger_reason,
            bot_response=bot_response,
        )

    async def _increment_gap(self, gap_id: int) -> None:
        """Bump the repeat counter and last-seen stamp of an existing gap."""
        async with self.db_manager.session() as session:
            await session.execute(
                UPDATE_GAP_COUNT_SQL,
                {"id": gap_id, "last_seen": datetime.now(UTC)},
            )
        logger.debug("Incremented gap count for existing gap id=%d", gap_id)

    async def _find_gap_by_query(self, user_query: str) -> int | None:
        """Find a gap recorded under exactly this query text.

        Used when the query could not be embedded: an exact repeat is the one
        duplicate that can still be recognised without a vector.
        """
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    FIND_GAP_BY_QUERY_SQL,
                    {"user_query": user_query},
                )
                row = result.fetchone()
                return int(row.id) if row else None
        except Exception as e:
            logger.warning("Failed to search knowledge gaps by query text: %s", e)
            return None

    async def _find_similar_gap(self, vector_str: str) -> int | None:
        """Find an existing gap with cosine similarity above threshold."""
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    FIND_SIMILAR_GAP_SQL,
                    {"vector_str": vector_str},
                )
                row = result.fetchone()
                if row and float(row.similarity) >= DEDUP_SIMILARITY_THRESHOLD:
                    return int(row.id)
                return None
        except Exception as e:
            logger.warning("Failed to search similar knowledge gaps: %s", e)
            return None

    async def _insert_gap(
        self,
        user_query: str,
        vector_str: str | None,
        telegram_user_id: int,
        best_faq_question: str | None,
        max_similarity: float,
        faq_count: int,
        trigger_reason: str,
        bot_response: str | None,
    ) -> None:
        """Insert a brand new knowledge gap record."""
        now = datetime.now(UTC)
        async with self.db_manager.session() as session:
            await session.execute(
                INSERT_GAP_SQL,
                {
                    "user_query": user_query,
                    "vector_str": vector_str,
                    "telegram_id": telegram_user_id,
                    "best_faq_question": best_faq_question,
                    "max_similarity": max_similarity,
                    "faq_count": faq_count,
                    "trigger_reason": trigger_reason,
                    "bot_response": bot_response,
                    "first_seen": now,
                    "last_seen": now,
                },
            )
        logger.debug(
            "Inserted new knowledge gap: trigger=%s, query='%s'",
            trigger_reason,
            user_query,
        )

    async def get_top_gaps(self, limit: int = DEFAULT_TOP_LIMIT) -> list[GapStatsDto]:
        """Fetch top knowledge gaps sorted by gap count descending."""
        safe_limit = max(1, min(limit, 100))
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    SELECT_TOP_GAPS_SQL,
                    {"limit": safe_limit},
                )
                rows = result.fetchall()
                return [
                    GapStatsDto(
                        user_query=str(row.user_query),
                        gap_count=int(row.gap_count),
                        trigger_reason=str(row.trigger_reason) if row.trigger_reason else None,
                        first_seen=row.first_seen,
                        last_seen=row.last_seen,
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.warning("Failed to get top knowledge gaps: %s", e)
            return []

    @staticmethod
    def _truncate(s: str, max_length: int) -> str:
        """Truncate string to maximum length."""
        return s[:max_length] if len(s) > max_length else s

    @classmethod
    def _truncate_optional(cls, s: str | None, max_length: int) -> str | None:
        """Truncate a string that may be absent."""
        return None if s is None else cls._truncate(s, max_length)
