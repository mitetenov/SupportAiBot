"""FAQ vector embedding service with PGVector hybrid search and Reciprocal Rank Fusion."""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence

from sqlalchemy import text

from app.logging_config import TRACE, log_failure
from app.rag.embedding import EmbeddingProvider
from app.rag.types import FaqContext, FaqEntry, FaqResult
from app.storage.database import DatabaseSessionManager
from app.storage.schema import FAQ_FTS_EXPRESSION, ensure_faq_search_schema

logger = logging.getLogger(__name__)

#: Rows each hybrid search returns. Left at 3 deliberately: measured over
#: benchmarks/, raising it to 5 does not change rank 1 on a single query and
#: changes recall on none, while every extra row is FAQ text in the prompt of
#: every request.
SEARCH_LIMIT: int = 3
MAX_RESULTS: int = 5
#: Floor for the vector branch. 0.65 was never measured and turned out to be far
#: too strict for the short imperative questions people actually send ("как
#: оплатить", "Триал есть?"): it retrieved the right entry for half of them.
#: 0.45 takes that to 93% while the off-topic controls in benchmarks/ still match
#: nothing; junk first appears at 0.35.
MIN_VECTOR_SIMILARITY: float = 0.45
MIN_FTS_RANK: float = 0.01
RRF_K: int = 60
EMBEDDING_CACHE_SIZE: int = 256

#: How many rows each branch of the hybrid search contributes to the fusion.
#: Larger than SEARCH_LIMIT so the two rankings have something to disagree
#: about, small enough that both branches stay index-only.
CANDIDATE_LIMIT: int = 20

#: Texts sent to the embedding provider in one HTTP call while indexing.
EMBED_BATCH_SIZE: int = 32
INDEX_PREPARATION_VERSION: str = "faq-document-v2"

GLOBAL_SEARCH_ALIASES: list[str] = ["vpn", "впн", "вэпэн"]

CONNECTION_FAQ_QUERY: str = "Не могу подключиться к VPN / не работает / не заходит"
REFERRAL_FAQ_QUERY: str = (
    "Реферальная программа, партнёрка, реферальная ссылка, пригласить друга, бонусы, рефералы"
)

# Reciprocal Rank Fusion over two index-backed branches.
#
# The previous version scored every row in the table — cosine distance and
# ts_rank in the select list, RANK() over the whole result — so neither the HNSW
# index nor the GIN index could be used and the cost grew with the size of the
# FAQ. Each branch now stands on its own as an ``ORDER BY <=> ... LIMIT`` and an
# ``@@`` match, which is exactly the shape those indices serve; the fusion
# happens over the two short candidate lists.
HYBRID_SEARCH_SQL = text(f"""
    WITH vector_candidates AS (
        SELECT question,
               answer,
               image,
               1 - (embedding <=> CAST(:vector_str AS vector)) AS vector_sim
        FROM faq
        WHERE embedding IS NOT NULL
          AND NOT (question = ANY(CAST(:excluded AS text[])))
        ORDER BY embedding <=> CAST(:vector_str AS vector)
        LIMIT :candidates
    ),
    vector_ranked AS (
        SELECT question, answer, image, vector_sim,
               ROW_NUMBER() OVER (ORDER BY vector_sim DESC) AS vector_pos
        FROM vector_candidates
    ),
    fts_candidates AS (
        SELECT question,
               answer,
               image,
               ts_rank({FAQ_FTS_EXPRESSION}, websearch_to_tsquery('russian', :clean_query))
                   AS fts_rank
        FROM faq
        WHERE {FAQ_FTS_EXPRESSION} @@ websearch_to_tsquery('russian', :clean_query)
          AND NOT (question = ANY(CAST(:excluded AS text[])))
        ORDER BY fts_rank DESC
        LIMIT :candidates
    ),
    fts_ranked AS (
        SELECT question, answer, image, fts_rank,
               ROW_NUMBER() OVER (ORDER BY fts_rank DESC) AS fts_pos
        FROM fts_candidates
    ),
    fused AS (
        SELECT COALESCE(v.question, f.question) AS question,
               COALESCE(v.answer, f.answer)     AS answer,
               COALESCE(v.image, f.image)       AS image,
               COALESCE(v.vector_sim, 0)        AS vector_sim,
               COALESCE(f.fts_rank, 0)          AS fts_rank,
               COALESCE(1.0 / (:rrf_k + v.vector_pos), 0)
               + COALESCE(1.0 / (:rrf_k + f.fts_pos), 0) AS rrf_score
        FROM vector_ranked v
        FULL OUTER JOIN fts_ranked f ON v.question = f.question
    )
    SELECT question, answer, image, vector_sim, fts_rank, rrf_score
    FROM fused
    WHERE vector_sim >= :min_vector_sim OR fts_rank >= :min_fts_rank
    ORDER BY rrf_score DESC
    LIMIT :limit
""")

INSERT_FAQ_SQL = text("""
    INSERT INTO faq (id, question, answer, embedding, keywords, image)
    VALUES (:id, :question, :answer, CAST(:vector_str AS vector), :keywords, :image)
""")

VECTOR_SEARCH_SQL = text("""
    SELECT question, answer, image,
           1 - (embedding <=> CAST(:vector_str AS vector)) AS vector_sim
    FROM faq
    WHERE embedding IS NOT NULL
      AND NOT (question = ANY(CAST(:excluded AS text[])))
    ORDER BY embedding <=> CAST(:vector_str AS vector)
    LIMIT :limit
""")

# A healthy PostgreSQL full-text index can still answer common support queries
# while the external embedding provider is rate-limited or unavailable.  Keep
# this query independent of pgvector so it is a real degradation path rather
# than another call that needs an embedding first.
FTS_SEARCH_SQL = text(f"""
    SELECT question,
           answer,
           image,
           ts_rank({FAQ_FTS_EXPRESSION}, websearch_to_tsquery('russian', :clean_query))
               AS fts_rank
    FROM faq
    WHERE {FAQ_FTS_EXPRESSION} @@ websearch_to_tsquery('russian', :clean_query)
      AND NOT (question = ANY(CAST(:excluded AS text[])))
    ORDER BY fts_rank DESC
    LIMIT :limit
""")


class FaqEmbeddingService:
    """Service for indexing, hybrid searching, and managing FAQ vector embeddings."""

    def __init__(
        self,
        db_manager: DatabaseSessionManager,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.db_manager = db_manager
        self.embedding_provider = embedding_provider
        self.ready: bool = False
        self.embedding_cache: OrderedDict[str, list[float]] = OrderedDict()

    def is_ready(self) -> bool:
        """Return whether the FAQ service is initialized and ready for searching."""
        return self.ready

    def mark_ready(self) -> None:
        """Mark the FAQ service as ready for search queries."""
        self.ready = True
        logger.info("FAQ service marked as ready for search")

    async def init_schema(self) -> None:
        """Bring the FAQ table in line with the configured embedding provider.

        Tables and columns come from the ORM models via ``create_all``; only the
        vector dimension and the search indices are settled here, because
        neither is known before the provider exists.
        """
        logger.info("Initializing FAQ database schema")
        async with self.db_manager.session() as session:
            await ensure_faq_search_schema(session, self.embedding_provider.get_dimension())
        logger.info("FAQ database schema initialized successfully")

    async def get_faq_hash(self) -> str | None:
        """Get the stored SHA-256 hash of the indexed FAQ file."""
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    text("SELECT val FROM faq_metadata WHERE key = 'faq_hash'")
                )
                row = result.fetchone()
                return str(row[0]) if row else None
        except Exception as e:
            logger.error("Failed to fetch FAQ hash (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "Failed to fetch FAQ hash exception: %s", e, exc_info=True)
            return None

    async def get_faq_index_fingerprint(self) -> str | None:
        """Return the version of the FAQ data and embedding representation."""
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    text("SELECT val FROM faq_metadata WHERE key = 'faq_index_fingerprint'")
                )
                row = result.fetchone()
                return str(row[0]) if row else None
        except Exception as e:
            logger.error("Failed to fetch FAQ index fingerprint (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE, "Failed to fetch FAQ index fingerprint exception: %s", e, exc_info=True
                )
            return None

    async def update_faq_hash(self, hash_val: str) -> None:
        """Update the stored SHA-256 hash of the indexed FAQ file."""
        async with self.db_manager.session() as session:
            await session.execute(
                text("""
                INSERT INTO faq_metadata (key, val) VALUES ('faq_hash', :hash_val)
                ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val
                """),
                {"hash_val": hash_val},
            )

    async def update_faq_index_fingerprint(self, fingerprint: str) -> None:
        """Store the fingerprint that guarantees query and document vector compatibility."""
        async with self.db_manager.session() as session:
            await session.execute(
                text("""
                INSERT INTO faq_metadata (key, val) VALUES ('faq_index_fingerprint', :fingerprint)
                ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val
                """),
                {"fingerprint": fingerprint},
            )

    def get_index_fingerprint(self, faq_hash: str) -> str:
        """Fingerprint source data and every setting that changes its vectors.

        Dimension alone is insufficient: two embedding models can produce the
        same-sized vectors in incompatible spaces.  This intentionally contains
        no credential and can be safely logged or compared across deployments.
        """
        provider = self.embedding_provider
        payload = {
            "faq_hash": faq_hash,
            "preparation": INDEX_PREPARATION_VERSION,
            "provider": f"{type(provider).__module__}.{type(provider).__qualname__}",
            "model": getattr(provider, "model", getattr(provider, "MODEL", None)),
            "base_url": getattr(provider, "base_url", getattr(provider, "DEFAULT_BASE_URL", None)),
            "dimension": provider.get_dimension(),
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    async def get_faq_count(self) -> int:
        """Get the number of indexed FAQ rows in PostgreSQL."""
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(text("SELECT COUNT(*) FROM faq"))
                row = result.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error("Failed to get FAQ count (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "Failed to get FAQ count exception: %s", e, exc_info=True)
            return 0

    async def get_indexed_faq_count(self) -> int:
        """Count FAQ rows that actually carry an embedding and are therefore searchable."""
        try:
            async with self.db_manager.session() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM faq WHERE embedding IS NOT NULL")
                )
                row = result.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error("Failed to count indexed FAQ rows (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE, "Failed to count indexed FAQ rows exception: %s", e, exc_info=True
                )
            return 0

    async def clear_faq(self) -> None:
        """Delete all entries from the FAQ table and mark service unready."""
        self.ready = False
        async with self.db_manager.session() as session:
            await session.execute(text("DELETE FROM faq"))
        logger.info("Cleared all FAQ table entries")

    @classmethod
    def with_global_aliases(cls, keywords: str | None) -> str:
        """Append global search aliases to keywords to boost Russian query recall."""
        base = f"{keywords.strip()}, " if keywords and keywords.strip() else ""
        return base + ", ".join(GLOBAL_SEARCH_ALIASES)

    @classmethod
    def vector_to_string(cls, vector: list[float]) -> str:
        """Convert a list of floats to pgvector string literal representation."""
        return "[" + ",".join(str(x) for x in vector) + "]"

    async def embed(self, text_input: str) -> list[float]:
        """Produce an embedding vector with LRU caching."""
        if not text_input or not text_input.strip():
            return []

        if text_input in self.embedding_cache:
            self.embedding_cache.move_to_end(text_input)
            return self.embedding_cache[text_input]

        vec = await self.embedding_provider.embed(text_input)
        if not vec or len(vec) != self.embedding_provider.get_dimension():
            return []

        self._cache(text_input, vec)
        return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed several texts in one provider call, serving what the cache holds.

        Returns one vector per input, in order; an entry the provider could not
        embed comes back as an empty list.
        """
        if not texts:
            return []

        dimension = self.embedding_provider.get_dimension()
        results: list[list[float]] = [[] for _ in texts]
        pending: list[tuple[int, str]] = []

        for position, item in enumerate(texts):
            if not item or not item.strip():
                continue
            cached = self.embedding_cache.get(item)
            if cached is not None:
                self.embedding_cache.move_to_end(item)
                results[position] = cached
            else:
                pending.append((position, item))

        if not pending:
            return results

        vectors = await self.embedding_provider.embed_batch([item for _, item in pending])
        for (position, item), vector in zip(pending, vectors, strict=False):
            if not vector or len(vector) != dimension:
                continue
            results[position] = vector
            self._cache(item, vector)

        return results

    def _cache(self, key: str, vector: list[float]) -> None:
        """Store a vector, evicting the least recently used entry when full."""
        self.embedding_cache[key] = vector
        self.embedding_cache.move_to_end(key)
        while len(self.embedding_cache) > EMBEDDING_CACHE_SIZE:
            self.embedding_cache.popitem(last=False)

    async def embed_query(self, text_input: str) -> list[float] | None:
        """Embed query and return list or None on failure."""
        vec = await self.embed(text_input)
        return vec if vec else None

    async def embed_query_as_vector(self, text_input: str) -> str | None:
        """Embed query and format as pgvector literal string or None."""
        vec = await self.embed(text_input)
        return self.vector_to_string(vec) if vec else None

    async def index_faq(
        self,
        question: str,
        answer: str,
        keywords: str | None,
        image: str | None = None,
    ) -> None:
        """Embed and insert a single FAQ item into PostgreSQL."""
        searchable = self.with_global_aliases(keywords)
        embedding = await self.embed(self.embed_text(question, answer, searchable))

        if not embedding or len(embedding) != self.embedding_provider.get_dimension():
            log_failure(logger, "FAQ embedding failed", details={"question": question})
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "Failed to embed FAQ full question: %s", question)
            return

        async with self.db_manager.session() as session:
            await session.execute(
                INSERT_FAQ_SQL,
                self._faq_row(question, answer, searchable, embedding, image),
            )
        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "Indexed FAQ: %s with keywords: %s", question, searchable)

    async def index_faq_batch(self, entries: Sequence[FaqEntry]) -> int:
        """Embed and insert every entry, and return how many were indexed.

        Startup used to walk the file one entry at a time: an HTTP round trip to
        the embedding provider and a transaction of its own per question, with
        the bot not yet answering anyone. The requests now go out in batches and
        the rows land in a single INSERT.
        """
        if not entries:
            return 0

        rows = await self._rows_for_entries(entries)

        if not rows:
            logger.error("No FAQ entry could be embedded — search will find nothing")
            return 0

        async with self.db_manager.session() as session:
            await session.execute(INSERT_FAQ_SQL, rows)

        logger.info("Indexed %d of %d FAQ entries", len(rows), len(entries))
        return len(rows)

    async def replace_faq_batch(self, entries: Sequence[FaqEntry]) -> int:
        """Atomically replace FAQ rows only after every new embedding is ready.

        Embeddings are requested before the transaction.  A provider outage or
        partial batch therefore leaves the currently searchable FAQ untouched.
        """
        if not entries:
            return 0

        rows = await self._rows_for_entries(entries)
        if len(rows) != len(entries):
            logger.error(
                "FAQ replacement aborted: embedded %d of %d entries; keeping the active index",
                len(rows),
                len(entries),
            )
            return len(rows)

        async with self.db_manager.session() as session:
            await session.execute(text("DELETE FROM faq"))
            await session.execute(INSERT_FAQ_SQL, rows)

        logger.info("Atomically replaced FAQ index with %d entries", len(rows))
        return len(rows)

    async def _rows_for_entries(self, entries: Sequence[FaqEntry]) -> list[dict[str, object]]:
        """Embed FAQ entries before choosing whether to insert or replace them."""
        dimension = self.embedding_provider.get_dimension()
        rows: list[dict[str, object]] = []

        for start in range(0, len(entries), EMBED_BATCH_SIZE):
            chunk = list(entries[start : start + EMBED_BATCH_SIZE])
            searchables = [self.with_global_aliases(entry.keywords) for entry in chunk]
            vectors = await self.embed_many(
                [
                    self.embed_text(entry.question, entry.answer, searchable)
                    for entry, searchable in zip(chunk, searchables, strict=True)
                ]
            )

            for entry, searchable, vector in zip(chunk, searchables, vectors, strict=True):
                if not vector or len(vector) != dimension:
                    log_failure(
                        logger, "FAQ embedding failed", details={"question": entry.question}
                    )
                    if logger.isEnabledFor(TRACE):
                        logger.log(TRACE, "Failed to embed FAQ full question: %s", entry.question)
                    continue
                rows.append(
                    self._faq_row(entry.question, entry.answer, searchable, vector, entry.image)
                )

        return rows

    @staticmethod
    def embed_text(question: str, answer: str, searchable_keywords: str) -> str:
        """Build the document handed to the embedding provider for one entry."""
        return f"{question} {searchable_keywords}\n{answer}"

    def _faq_row(
        self,
        question: str,
        answer: str,
        keywords: str,
        embedding: list[float],
        image: str | None = None,
    ) -> dict[str, object]:
        """Build the parameter set for one INSERT_FAQ_SQL row."""
        return {
            "id": str(uuid.uuid4()),
            "question": question,
            "answer": answer,
            "vector_str": self.vector_to_string(embedding),
            "keywords": keywords,
            "image": image,
        }

    async def search(self, query: str, exclude: set[str] | None = None) -> list[FaqResult]:
        """Perform hybrid search combining vector cosine distance and Russian FTS with RRF.

        ``exclude`` is applied inside the query rather than to its output: the
        LIMIT has to be spent on entries the user has not been shown yet,
        otherwise filtering afterwards just empties an already-small result set.
        """
        if not self.ready or not query or not query.strip():
            return []

        start_time = time.monotonic()
        raw_clean = re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", " ", query).strip()
        clean_query = raw_clean if raw_clean else query
        vector_str = await self.embed_query_as_vector(query)
        if not vector_str:
            logger.info("FAQ embedding unavailable; degrading to FTS-only search")
            return await self._search_fts_only(clean_query, exclude)

        try:
            params = {
                "vector_str": vector_str,
                "clean_query": clean_query,
                "rrf_k": RRF_K,
                "min_vector_sim": MIN_VECTOR_SIMILARITY,
                "min_fts_rank": MIN_FTS_RANK,
                "limit": SEARCH_LIMIT,
                "candidates": CANDIDATE_LIMIT,
                "excluded": sorted(exclude) if exclude else [],
            }
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "RAG hybrid search SQL: %s, params: %s",
                    HYBRID_SEARCH_SQL,
                    {"clean_query": clean_query, "excluded": params["excluded"]},
                )
            async with self.db_manager.session() as session:
                result = await session.execute(HYBRID_SEARCH_SQL, params)
                rows = result.fetchall()
                results = [
                    FaqResult(
                        question=str(row.question),
                        answer=str(row.answer),
                        similarity=float(row.vector_sim),
                        rrf_score=float(row.rrf_score),
                        image=str(row.image) if row.image else None,
                    )
                    for row in rows
                ]
                duration = time.monotonic() - start_time
                logger.info(
                    "RAG search: operation=hybrid_search, candidates_count=%d, outcome=success, duration=%.3fs",
                    len(results),
                    duration,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "RAG hybrid search candidates: %s",
                        [r.question for r in results],
                    )
                return results
        except Exception as e:
            duration = time.monotonic() - start_time
            log_failure(logger, "RAG hybrid search failed", e)
            logger.info(
                "FAQ hybrid search degraded to pure vector search (reason=%s)",
                type(e).__name__,
            )
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "FAQ hybrid search failure details: %s", e, exc_info=True)
            return await self._search_pure_vector(vector_str, exclude)

    async def _search_fts_only(
        self, clean_query: str, exclude: set[str] | None = None
    ) -> list[FaqResult]:
        """Search the PostgreSQL text index when no query embedding is available."""
        start_time = time.monotonic()
        try:
            params = {
                "clean_query": clean_query,
                "limit": SEARCH_LIMIT,
                "excluded": sorted(exclude) if exclude else [],
            }
            async with self.db_manager.session() as session:
                result = await session.execute(FTS_SEARCH_SQL, params)
                rows = result.fetchall()
            results = [
                FaqResult(
                    question=str(row.question),
                    answer=str(row.answer),
                    similarity=0.0,
                    rrf_score=float(row.fts_rank),
                    image=str(row.image) if row.image else None,
                )
                for row in rows
                if float(row.fts_rank) >= MIN_FTS_RANK
            ]
            logger.info(
                "RAG search: operation=fts_only_search, candidates_count=%d, outcome=success, duration=%.3fs",
                len(results),
                time.monotonic() - start_time,
            )
            return results
        except Exception as e:
            logger.error("FAQ FTS-only search failed (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "FAQ FTS-only search exception: %s", e, exc_info=True)
            return []

    async def _search_pure_vector(
        self, vector_str: str, exclude: set[str] | None = None
    ) -> list[FaqResult]:
        """Fallback to pure vector cosine search when FTS or hybrid fails."""
        start_time = time.monotonic()
        try:
            params = {
                "vector_str": vector_str,
                "limit": SEARCH_LIMIT,
                "excluded": sorted(exclude) if exclude else [],
            }
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "RAG vector search SQL: %s, params: %s",
                    VECTOR_SEARCH_SQL,
                    {"excluded": params["excluded"]},
                )
            async with self.db_manager.session() as session:
                result = await session.execute(VECTOR_SEARCH_SQL, params)
                rows = result.fetchall()
                results: list[FaqResult] = []
                for row in rows:
                    similarity = float(row.vector_sim)
                    if similarity >= MIN_VECTOR_SIMILARITY:
                        results.append(
                            FaqResult(
                                question=str(row.question),
                                answer=str(row.answer),
                                similarity=similarity,
                                rrf_score=similarity,
                                image=str(row.image) if row.image else None,
                            )
                        )
                duration = time.monotonic() - start_time
                logger.info(
                    "RAG search: operation=pure_vector_search, candidates_count=%d, outcome=success, duration=%.3fs",
                    len(results),
                    duration,
                )
                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "RAG vector search candidates: %s",
                        [r.question for r in results],
                    )
                return results
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.error("FAQ pure vector search failed (error_class=%s)", type(e).__name__)
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "FAQ pure vector search exception: %s", e, exc_info=True)
            return []

    async def search_with_fallback(
        self, query: str, exclude: set[str] | None = None
    ) -> list[FaqResult]:
        """Search, widening with topic fallbacks, and merge the results.

        The fallback lookups do not depend on the primary one, so all of them go
        to the database at once rather than adding a round trip each to the
        answer the user is waiting for.

        Fallback hits are appended below the primary ranking rather than sorted
        in with it. A canned topic query matches its own FAQ entry almost
        exactly, so on a shared RRF scale it outscores whatever the user asked
        about — and the keyword lists that trigger these searches are broad
        enough ("подписк", "обнов", "ошибк") to fire on most support questions.
        Measured over the queries in benchmarks/, letting them compete costs 25
        points of rank-1 accuracy and buys no recall. They can still fill
        positions the primary search left empty, which is what they are for.
        """
        searches = [self.search(query, exclude)]
        if self._looks_like_connection_issue(query):
            searches.append(self.search(CONNECTION_FAQ_QUERY, exclude))
        if self._looks_like_referral_query(query):
            searches.append(self.search(REFERRAL_FAQ_QUERY, exclude))

        primary, *fallbacks = await asyncio.gather(*searches)

        results = self._by_score(primary)
        for fallback in fallbacks:
            self._merge_deduped(results, self._by_score(fallback))
        return results[:MAX_RESULTS]

    @staticmethod
    def _by_score(results: Sequence[FaqResult]) -> list[FaqResult]:
        """One search's hits, best RRF score first."""
        return sorted(results, key=lambda r: r.rrf_score, reverse=True)

    @staticmethod
    def _merge_deduped(target: list[FaqResult], source: list[FaqResult]) -> None:
        """Merge source results into target list without duplicate question titles."""
        existing_questions = {r.question for r in target}
        for item in source:
            if item.question not in existing_questions:
                existing_questions.add(item.question)
                target.append(item)

    async def build_faq_context(
        self,
        user_query: str,
        exclude_questions: set[str] | None = None,
    ) -> FaqContext:
        """Retrieve FAQ entries for LLM context, filter excluded questions, and format instructions."""
        results = await self.search_with_fallback(user_query, exclude_questions)
        if exclude_questions:
            results = [r for r in results if r.question not in exclude_questions]

        if not results:
            return FaqContext.EMPTY

        sb = (
            "Кандидаты FAQ (проверь соответствие вопросу, истории и фактам инструментов; "
            "разрешено кратко изложить подходящую часть с сохранением точных названий, "
            "ограничений, условий и порядка шагов; если кандидаты не подходят, уточни "
            "проблему, не давай нерелевантных инструкций):\n"
        )
        for r in results:
            sb += f"Вопрос: {r.question}\nИнструкция: {r.answer}\n\n"

        max_similarity = max((r.similarity for r in results), default=0.0)

        return FaqContext(
            text=sb,
            results=results,
            max_similarity=max_similarity,
            best_question=results[0].question,
        )

    @staticmethod
    def _looks_like_connection_issue(query: str | None) -> bool:
        """Determine if the query describes a VPN connection, server, or speed issue."""
        if not query or not query.strip():
            return False
        lower = query.lower()
        keywords = (
            "подключ",
            "не работ",
            "не заход",
            "vpn",
            "впн",
            "скорост",
            "медлен",
            "сайт",
            "instagram",
            "ошибк",
            "отвали",
            "обрыв",
            "обнов",
            "подписк",
            "пинг",
            "сервер",
        )
        return any(kw in lower for kw in keywords)

    @staticmethod
    def _looks_like_referral_query(query: str | None) -> bool:
        """Determine if the query asks about referral program or partner bonuses."""
        if not query or not query.strip():
            return False
        lower = query.lower()
        keywords = (
            "реферал",
            "партнёр",
            "партнер",
            "partner",
            "приглас",
            "приглаш",
            "друг",
            "друз",
            "бонус",
        )
        return any(kw in lower for kw in keywords)
