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
    #: True when this query's exact wording was added to an FAQ entry's keywords.
    #: Such a case scores that edit, not the retrieval settings, so it is kept
    #: out of the headline number.
    tuned_for: bool = False

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
    #: Same counters over the cases whose wording was not used for keyword edits.
    held_hit_at_1: int = 0
    held_hit_at_k: int = 0
    held_positives: int = 0

    def line(self, min_sim: float, limit: int) -> str:
        def pct(n: int, d: int) -> str:
            return f"{n / d:>6.0%}" if d else "     -"

        return (
            f"{min_sim:>5.2f} {limit:>5}  "
            f"{self.held_hit_at_1:>2}/{self.held_positives:<2} {pct(self.held_hit_at_1, self.held_positives)}  "
            f"{self.held_hit_at_k:>2}/{self.held_positives:<2} {pct(self.held_hit_at_k, self.held_positives)}  "
            f"{pct(self.hit_at_1, self.positives)}  {pct(self.hit_at_k, self.positives)}  "
            f"{self.control_false_matches:>2}/{self.controls:<2} {pct(self.control_false_matches, self.controls)}"
        )


def load_cases() -> list[Case]:
    payload = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return [Case(**c) for c in payload["cases"]]


_real_connection = FaqEmbeddingService._looks_like_connection_issue
_real_referral = FaqEmbeddingService._looks_like_referral_query
_real_search_with_fallback = FaqEmbeddingService.search_with_fallback

#: How the topic fallbacks behave in a given run.
#:  "compete" — what ships: fallback hits are merged in and everything is
#:              re-sorted by RRF score, so a canned query that matches its own
#:              FAQ entry almost exactly can take rank 1 from the user's actual
#:              question.
#:  "append"  — fallback hits keep the primary ranking above them: they can only
#:              fill positions the primary search left empty.
#:  "off"     — no fallback search at all.
FallbackMode = str
FALLBACK_MODES: tuple[FallbackMode, ...] = ("compete", "append", "off")


async def _search_appending(
    self: FaqEmbeddingService,
    query: str,
    exclude: set[str] | None = None,
) -> list[rag.FaqResult]:
    """search_with_fallback, but fallback hits never outrank the primary ones."""
    searches = [self.search(query, exclude)]
    if _real_connection(query):
        searches.append(self.search(rag.CONNECTION_FAQ_QUERY, exclude))
    if _real_referral(query):
        searches.append(self.search(rag.REFERRAL_FAQ_QUERY, exclude))

    primary, *fallbacks = await asyncio.gather(*searches)

    results = sorted(primary, key=lambda r: r.rrf_score, reverse=True)
    for fallback in fallbacks:
        self._merge_deduped(results, sorted(fallback, key=lambda r: r.rrf_score, reverse=True))
    return results[: rag.MAX_RESULTS]


def set_fallback_mode(mode: FallbackMode) -> None:
    """Install one of the three fallback behaviours for the next run."""
    predicate_on = mode != "off"
    FaqEmbeddingService._looks_like_connection_issue = staticmethod(  # type: ignore[method-assign]
        _real_connection if predicate_on else lambda query: False
    )
    FaqEmbeddingService._looks_like_referral_query = staticmethod(  # type: ignore[method-assign]
        _real_referral if predicate_on else lambda query: False
    )
    FaqEmbeddingService.search_with_fallback = (  # type: ignore[method-assign]
        _searching_appending_or_real(mode)
    )


def _searching_appending_or_real(mode: FallbackMode):  # type: ignore[no-untyped-def]
    return _search_appending if mode == "append" else _real_search_with_fallback


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
        first = top in case.expected
        within = any(q in case.expected for q in found)
        if first:
            score.hit_at_1 += 1
        if within:
            score.hit_at_k += 1
        else:
            score.missed += 1

        if not case.tuned_for:
            score.held_positives += 1
            score.held_hit_at_1 += int(first)
            score.held_hit_at_k += int(within)

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
        "--detail-mode",
        choices=FALLBACK_MODES,
        default="append",
        help="which fallback mode the per-query detail comes from",
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
    held = sum(1 for c in cases if c.expected and not c.tuned_for)
    tuned = sum(1 for c in cases if c.tuned_for)
    print(
        f"  {held} of the labelled queries are held out of keyword tuning; "
        f"{tuned} had their wording used and are shown only in the 'all' columns.\n"
    )
    print("  sim limit   held-out hit@1  held-out hit@k    all@1   all@k   off-topic")
    print("  " + "-" * 74)

    labels = {
        "compete": "fallbacks compete for rank 1 (ships today)",
        "append": "fallbacks append below the primary ranking",
        "off": "no fallbacks",
    }
    details: dict[float, list[tuple[Case, str | None]]] = {}
    for mode in FALLBACK_MODES:
        set_fallback_mode(mode)
        print(f"\n  {labels[mode]}")
        for min_sim, limit in GRID:
            score, detail = await score_grid_point(faq_service, cases, min_sim, limit)
            marker = (
                "  <- ships today"
                if mode == "compete" and (min_sim, limit) == (baseline_sim, baseline_limit)
                else ""
            )
            print("  " + score.line(min_sim, limit) + marker)
            if limit == 5 and mode == args.detail_mode:
                details[min_sim] = detail
    set_fallback_mode("compete")

    if args.detail_at is not None and args.detail_at in details:
        print(
            f"\nPer-query at MIN_VECTOR_SIMILARITY={args.detail_at}, SEARCH_LIMIT=5, "
            f"fallbacks {args.detail_mode}:\n"
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
