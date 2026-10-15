from unittest.mock import AsyncMock, MagicMock

import pytest

from app.rag.catalog import FaqCatalog, format_faq_context
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqResult


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,required",
    [
        ("дайте ссылку на подписку", {"subscription_link"}),
        ("как подключить второй телефон", {"device_setup"}),
        ("все серверы n/a, Android", {"na_diagnosis", "na_apple", "na_windows_android"}),
        (r"все серверы n\a, Incy", {"na_diagnosis", "na_apple", "na_windows_android"}),
        ("где кнопка обновления", {"app_buttons"}),
        ("как включить автоплатеж", {"auto_renewal"}),
        ("сколько трафика на пробу", {"trial"}),
        ("получателю подарка нужен триал", {"gift_recipient"}),
    ],
)
async def test_required_instructions_survive_empty_retrieval(
    query: str, required: set[str]
) -> None:
    catalog = FaqCatalog.load()
    service = FaqEmbeddingService(MagicMock(), MagicMock())
    service.catalog = catalog
    service.search_with_fallback = AsyncMock(return_value=[])
    context = await service.build_faq_context(query)
    for key in required:
        entry = catalog.by_id[key]
        assert entry["question"] in context.questions()
        # Local references do not pretend to be measured semantic matches.
        assert next(r for r in context.results if r.question == entry["question"]).similarity == 0


def test_payment_complaints_do_not_force_device_setup() -> None:
    catalog = FaqCatalog.load()
    assert catalog.required_ids("как подключить оплату в боте") == []
    assert catalog.required_ids("не подключается функция оплата") == []


def test_catalog_does_not_duplicate_retrieved_or_rejected_articles() -> None:
    catalog = FaqCatalog.load()
    entry = catalog.by_id["subscription_link"]
    result = FaqResult(entry["question"], entry["answer"], 0.8, 0.02)
    assert catalog.supplement("ссылка подписки", [result]) == [result]
    assert catalog.supplement("ссылка подписки", [], {entry["question"]}) == []


def test_shared_prerequisites_are_kept_once_and_article_tails_stay_ordered() -> None:
    prefix = (
        "Сначала выполните проверку обычного интернета. " * 5
        + "\n\n"
        + "Дождитесь результата. " * 8
    )
    results = [
        FaqResult("Apple", prefix + "\n\nУстановите Incy.", 0.8, 0.03, "happ-buttons.png"),
        FaqResult("Windows", prefix + "\n\nОбновите Happ.", 0.7, 0.02),
    ]
    original = [r.answer for r in results]
    context = format_faq_context(results)
    assert context.count(prefix) == 1
    assert context.count("Сначала общие шаги 1.") == 2
    assert (
        context.index(prefix) < context.index("Установите Incy.") < context.index("Обновите Happ.")
    )
    assert [r.answer for r in results] == original
    assert results[0].image == "happ-buttons.png"
    assert len(context) < sum(len(r.answer) for r in results)


def test_different_conditions_are_not_deduplicated() -> None:
    body = "Проверьте подписку. " * 30
    results = [
        FaqResult("A", "Если это Windows.\n\n" + body + "\n\nПервый шаг.", 0.9, 0.03),
        FaqResult("B", "Если это Android.\n\n" + body + "\n\nДругой шаг.", 0.8, 0.02),
    ]
    context = format_faq_context(results)
    for result in results:
        assert result.answer in context
    assert "Общие первые шаги" not in context


def test_duplicate_reference_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate FAQ id"):
        FaqCatalog([{"id": "same"}, {"id": "same"}])


def test_missing_or_invalid_source_preserves_search_results(tmp_path) -> None:
    path = tmp_path / "faq.json"
    result = FaqResult("Existing", "Retained index", 0.8, 0.02)
    assert FaqCatalog.load(path).supplement("ссылка подписки", [result]) == [result]
    path.write_text("{broken json")
    assert FaqCatalog.load(path).supplement("ссылка подписки", [result]) == [result]
