"""Data models and result types for FAQ retrieval and embeddings."""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class FaqResult:
    """A single retrieved FAQ entry."""

    question: str
    answer: str
    similarity: float
    rrf_score: float


@dataclass(frozen=True)
class FaqContext:
    """Retrieved FAQ entries and formatting for LLM context."""

    text: str
    results: list[FaqResult]
    max_similarity: float
    best_question: str | None

    EMPTY: ClassVar["FaqContext"]

    def is_empty(self) -> bool:
        """Return True if no FAQ results were matched."""
        return len(self.results) == 0

    def questions(self) -> set[str]:
        """Return set of question titles in order."""
        return {r.question for r in self.results}


FaqContext.EMPTY = FaqContext(text="", results=[], max_similarity=0.0, best_question=None)
