"""Sweeps the FAQ retrieval thresholds over real user queries.

Drives the production path — ``FaqEmbeddingService.build_faq_context`` — so what
is measured is what the model would have been handed, fallbacks and fusion
included. Needs the live pgvector and an embedding key, so it is a script and
not part of the pytest suite.

    python -m benchmarks.retrieval_benchmark [--reindex]
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.rag import service as rag
from app.rag.embedding import create_embedding_provider
from app.rag.initializer import FaqInitializer
from app.rag.service import FaqEmbeddingService
from app.storage.database import get_db_manager

QUERIES_PATH = Path(__file__).with_name("retrieval_queries.json")

#: (MIN_VECTOR_SIMILARITY, SEARCH_LIMIT) pairs to compare. The first is what
#: ships today.
GRID: list[tuple[float, int]] = [
    (0.65, 3),
    (0.60, 3),
    (0.55, 3),
    (0.50, 3),
    (0.45, 3),
    (0.65, 5),
    (0.60, 5),
    (0.55, 5),
    (0.50, 5),
    (0.45, 5),
    (0.40, 5),
    (0.35, 5),
]


@dataclass
class Case:
    query: str
    source: str
    expected: list[str]

    @property
    def is_control(self) -> bool:
        return not self.expected


@dataclass
class Score:
    hit_at_1: int = 0
    hit_at_k: int = 0
    missed: int = 0
    control_false_matches: int = 0
    positives: int = 0
    controls: int = 0

    def line(self, min_sim: float, limit: int) -> str:
        recall1 = self.hit_at_1 / self.positives if self.positives else 0.0
        recallk = self.hit_at_k / self.positives if self.positives else 0.0
        noise = self.control_false_matches / self.controls if self.controls else 0.0
        return (
            f"{min_sim:>5.2f} {limit:>5}  "
            f"{self.hit_at_1:>3}/{self.positives:<3} {recall1:>6.0%}  "
            f"{self.hit_at_k:>3}/{self.positives:<3} {recallk:>6.0%}  "
            f"{self.control_false_matches:>3}/{self.controls:<3} {noise:>6.0%}"
        )


def load_cases() -> list[Case]:
    payload = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return [Case(**c) for c in payload["cases"]]


def set_fallbacks(enabled: bool) -> None:
    """Turn the connection/referral topic fallbacks on or off.

    search_with_fallback runs an extra canned search whenever the query trips a
    keyword list, then re-sorts everything by RRF score. The canned query is a
    near-exact match for its own FAQ entry, so it scores higher than whatever
    the user actually asked about — the ablation measures how much rank 1 that
    costs.
    """
    if enabled:
        FaqEmbeddingService._looks_like_connection_issue = staticmethod(  # type: ignore[method-assign]
            _real_connection
        )
        FaqEmbeddingService._looks_like_referral_query = staticmethod(  # type: ignore[method-assign]
            _real_referral
        )
    else:
        FaqEmbeddingService._looks_like_connection_issue = staticmethod(  # type: ignore[method-assign]
            lambda query: False
        )
        FaqEmbeddingService._looks_like_referral_query = staticmethod(  # type: ignore[method-assign]
            lambda query: False
        )


_real_connection = FaqEmbeddingService._looks_like_connection_issue
_real_referral = FaqEmbeddingService._looks_like_referral_query


async def score_grid_point(
    faq_service: FaqEmbeddingService,
    cases: list[Case],
    min_sim: float,
    limit: int,
) -> tuple[Score, list[tuple[Case, str | None]]]:
    """Run every case at one grid point, returning the score and each top hit."""
    rag.MIN_VECTOR_SIMILARITY = min_sim
    rag.SEARCH_LIMIT = limit

    score = Score()
    detail: list[tuple[Case, str | None]] = []

    for case in cases:
        context = await faq_service.build_faq_context(case.query)
        found = [r.question for r in context.results]
        top = found[0] if found else None
        detail.append((case, top))

        if case.is_control:
            score.controls += 1
            if found:
                score.control_false_matches += 1
            continue

        score.positives += 1
        if top in case.expected:
            score.hit_at_1 += 1
        if any(q in case.expected for q in found):
            score.hit_at_k += 1
        else:
            score.missed += 1

    return score, detail


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="re-embed faq/faq.json before measuring (needed after editing it)",
    )
    parser.add_argument(
        "--detail-at",
        type=float,
        default=None,
        help="print per-query results for this MIN_VECTOR_SIMILARITY",
    )
    parser.add_argument(
        "--detail-fallbacks",
        action="store_true",
        help="take the per-query detail from the run with topic fallbacks on",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_manager = get_db_manager(settings.database_url)
    await db_manager.init_models()

    embedding_provider = create_embedding_provider(settings)
    faq_service = FaqEmbeddingService(
        db_manager=db_manager,
        embedding_provider=embedding_provider,
    )

    if args.reindex:
        await FaqInitializer(service=faq_service).run()
    else:
        await faq_service.init_schema()
        faq_service.mark_ready()

    cases = load_cases()
    baseline_sim, baseline_limit = GRID[0]

    print(
        f"\n{len(cases)} queries — {sum(1 for c in cases if not c.is_control)} labelled, "
        f"{sum(1 for c in cases if c.is_control)} off-topic controls\n"
    )
    print("  sim limit      hit@1           hit@k          off-topic noise")
    print("  " + "-" * 62)

    details: dict[float, list[tuple[Case, str | None]]] = {}
    for fallbacks in (True, False):
        set_fallbacks(fallbacks)
        print(f"\n  topic fallbacks: {'on (ships today)' if fallbacks else 'off'}")
        for min_sim, limit in GRID:
            score, detail = await score_grid_point(faq_service, cases, min_sim, limit)
            marker = (
                "  <- ships today"
                if fallbacks and (min_sim, limit) == (baseline_sim, baseline_limit)
                else ""
            )
            print("  " + score.line(min_sim, limit) + marker)
            if limit == 5 and fallbacks == args.detail_fallbacks:
                details[min_sim] = detail
    set_fallbacks(True)

    if args.detail_at is not None and args.detail_at in details:
        state = "on" if args.detail_fallbacks else "off"
        print(
            f"\nPer-query at MIN_VECTOR_SIMILARITY={args.detail_at}, SEARCH_LIMIT=5, "
            f"fallbacks {state}:\n"
        )
        for case, top in details[args.detail_at]:
            if case.is_control:
                verdict = "NOISE" if top else "ok   "
            else:
                verdict = "ok   " if top in case.expected else "MISS "
            print(f"  {verdict} [{case.source:>12}] {case.query[:64]!r}")
            if verdict.strip() != "ok":
                print(f"          got: {top!r}")

    await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
