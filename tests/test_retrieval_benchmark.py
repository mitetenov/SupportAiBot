"""Regression tests for the fallback strategies compared by the retrieval benchmark."""

import pytest

from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqResult
from benchmarks.retrieval_benchmark import _search_appending, _search_competing


class _SearchFixture:
    _merge_deduped = staticmethod(FaqEmbeddingService._merge_deduped)

    async def search(self, query: str, exclude: set[str] | None = None) -> list[FaqResult]:
        if query.startswith("Не могу подключиться"):
            return [FaqResult("Диагностика VPN", "fallback", 0.9, 0.03)]
        return [FaqResult("Вопрос пользователя", "primary", 0.6, 0.02)]


@pytest.mark.asyncio
async def test_benchmark_competing_and_appending_modes_have_distinct_rankings() -> None:
    fixture = _SearchFixture()

    competing = await _search_competing(fixture, "не работает vpn")
    appending = await _search_appending(fixture, "не работает vpn")

    assert [item.question for item in competing] == ["Диагностика VPN", "Вопрос пользователя"]
    assert [item.question for item in appending] == ["Вопрос пользователя", "Диагностика VPN"]
