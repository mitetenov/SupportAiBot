"""Data models and result types for FAQ retrieval and embeddings."""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class FaqEntry:
    """One question/answer pair on its way into the index."""

    question: str
    answer: str
    keywords: str | None = None
    #: File name under faq/images/ to send alongside the answer, when the entry
    #: is better shown than described.
    image: str | None = None


@dataclass(frozen=True)
class FaqResult:
    """A single retrieved FAQ entry."""

    question: str
    answer: str
    similarity: float
    rrf_score: float
    image: str | None = None


@dataclass(frozen=True)
class FaqContext:
    """Retrieved FAQ entries and formatting for LLM context."""

    text: str
    results: list[FaqResult]
    max_similarity: float
    best_question: str | None

    EMPTY: ClassVar[FaqContext]

    def is_empty(self) -> bool:
        """Return True if no FAQ results were matched."""
        return len(self.results) == 0

    def questions(self) -> set[str]:
        """Questions shown to the model, used as an exclusion set on the next turn."""
        return {r.question for r in self.results}


FaqContext.EMPTY = FaqContext(text="", results=[], max_similarity=0.0, best_question=None)
