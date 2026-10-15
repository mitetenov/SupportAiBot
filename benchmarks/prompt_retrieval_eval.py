"""Compare FAQ revisions on synthetic queries in a disposable local database.

Only FAQ text and the checked-in synthetic queries go to the embedding API.
The application database URL from .env is never used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url

import app.storage.models  # noqa: F401 - register models for init_models
from app.config import Settings
from app.rag.catalog import FaqCatalog, format_faq_context
from app.rag.embedding import EmbeddingProvider, create_embedding_provider
from app.rag.initializer import FaqInitializer
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqEntry, FaqResult
from app.storage.database import DatabaseSessionManager

ROOT = Path(__file__).resolve().parents[1]


def validate_database_url(value: str) -> str:
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in ("127.0.0.1", "localhost", "::1")
        or not (url.database or "").startswith("prompt_opt")
    ):
        raise ValueError("Use a disposable loopback PostgreSQL database named prompt_opt*")
    return value


class CachedEmbedding:
    """Reuse identical vectors across both revisions for a fair comparison."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider
        self.values: dict[str, list[float]] = {}

    def get_dimension(self) -> int:
        return self.provider.get_dimension()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        missing = list(dict.fromkeys(t for t in texts if t not in self.values))
        if missing:
            vectors = await self.provider.embed_batch(missing)
            if len(vectors) != len(missing) or any(not vector for vector in vectors):
                raise RuntimeError("Embedding failed; comparison is incomplete")
            self.values.update(zip(missing, vectors, strict=True))
        return [self.values[t] for t in texts]

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]


def baseline_context(results: list[FaqResult]) -> str:
    """Context formatter frozen from master eaad423 for size comparison."""
    if not results:
        return ""
    return (
        "Кандидаты FAQ (проверь соответствие вопросу, истории и фактам инструментов; "
        "разрешено кратко изложить подходящую часть с сохранением точных названий, "
        "ограничений, условий и порядка шагов; если кандидаты не подходят, уточни "
        "проблему, не давай нерелевантных инструкций):\n"
    ) + "".join(f"Вопрос: {r.question}\nИнструкция: {r.answer}\n\n" for r in results)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-faq", type=Path, required=True)
    parser.add_argument("--faq", type=Path, default=ROOT / "faq/faq.json")
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "benchmarks/prompt_retrieval_cases.json"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--database-url", type=validate_database_url, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confirm-external-api", action="store_true")
    args = parser.parse_args()
    if not args.confirm_external_api:
        parser.error("FAQ and synthetic queries require approval for the embedding API")
    if args.report.exists():
        parser.error("Choose a new report path; existing measurements are preserved")

    settings = Settings(
        _env_file=args.env_file,
        embedding_provider="openai",
        openai_base_url="https://api.openai.com/v1",
        openai_embedding_model="text-embedding-3-small",
    )
    cases = json.loads(args.cases.read_text())["cases"]
    report = {
        "model": settings.openai_embedding_model,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "runs": {},
    }
    db = DatabaseSessionManager(args.database_url)
    async with httpx.AsyncClient() as client:
        provider = CachedEmbedding(create_embedding_provider(settings, client))
        try:
            await db.init_models()
            for label, path in (("baseline", args.baseline_faq), ("candidate", args.faq)):
                entries = json.loads(path.read_text())
                service = FaqEmbeddingService(db, provider)
                await service.init_schema()
                usable = [
                    FaqEntry(
                        e["question"],
                        e["answer"],
                        FaqInitializer.extract_keywords(e.get("keywords")),
                        e.get("image"),
                    )
                    for e in entries
                ]
                if await service.replace_faq_batch(usable) != len(usable):
                    raise RuntimeError("Indexing failed; comparison is incomplete")
                service.mark_ready()
                catalog = FaqCatalog(entries)
                rows = []
                for case in cases:
                    raw = await service.search_with_fallback(case["query"])
                    selected = (
                        catalog.supplement(case["query"], raw) if label == "candidate" else raw
                    )
                    titles = [r.question for r in selected]
                    context = (
                        format_faq_context(selected)
                        if label == "candidate" and selected
                        else baseline_context(raw)
                    )
                    rows.append(
                        {
                            **case,
                            "titles": titles,
                            "raw_titles": [r.question for r in raw],
                            "context_chars": len(context),
                            "hit": bool(set(case["expected"]) & set(titles))
                            if case["expected"]
                            else not titles,
                            "top1": bool(titles and titles[0] in case["expected"]),
                        }
                    )
                report["runs"][label] = {
                    "faq_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "entries": len(entries),
                    "results": rows,
                    "correct": sum(r["hit"] for r in rows),
                    "top1": sum(r["top1"] for r in rows),
                    "context_chars": sum(r["context_chars"] for r in rows),
                }
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(
                    label,
                    {k: v for k, v in report["runs"][label].items() if k != "results"},
                    flush=True,
                )
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
