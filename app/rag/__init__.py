"""RAG subsystem: vector embeddings, hybrid PGVector search, FAQ sync, and knowledge gap tracking."""

from app.rag.embedding import (
    EmbeddingProvider,
    GeminiEmbeddingProvider,
    OpenAiEmbeddingProvider,
    create_embedding_provider,
)
from app.rag.initializer import FaqInitializer
from app.rag.knowledge_gaps import GapStatsDto, KnowledgeGapService
from app.rag.service import (
    CONNECTION_FAQ_QUERY,
    EMBEDDING_CACHE_SIZE,
    GLOBAL_SEARCH_ALIASES,
    MAX_RESULTS,
    MIN_FTS_RANK,
    MIN_VECTOR_SIMILARITY,
    REFERRAL_FAQ_QUERY,
    RRF_K,
    SEARCH_LIMIT,
    FaqContext,
    FaqEmbeddingService,
    FaqResult,
)

__all__ = [
    "CONNECTION_FAQ_QUERY",
    "EMBEDDING_CACHE_SIZE",
    "EmbeddingProvider",
    "FaqContext",
    "FaqEmbeddingService",
    "FaqInitializer",
    "FaqResult",
    "GLOBAL_SEARCH_ALIASES",
    "GapStatsDto",
    "GeminiEmbeddingProvider",
    "KnowledgeGapService",
    "MAX_RESULTS",
    "MIN_FTS_RANK",
    "MIN_VECTOR_SIMILARITY",
    "OpenAiEmbeddingProvider",
    "REFERRAL_FAQ_QUERY",
    "RRF_K",
    "SEARCH_LIMIT",
    "create_embedding_provider",
]
