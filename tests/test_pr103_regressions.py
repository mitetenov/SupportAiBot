"""Conversation and startup regressions found in the review of PR #103."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.llm.base import AbstractLlmClient, LlmProcessingException, LlmReply
from app.llm.deepseek import DeepSeekClient
from app.llm.fallback import LlmFallbackClient
from app.llm.gemini import GeminiClient
from app.rag.initializer import FaqInitializer
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqContext, FaqResult
from app.storage.chat_history import ChatHistoryService


def client(cls, history, rag):
    instance = object.__new__(cls)
    AbstractLlmClient.__init__(instance, MagicMock(), history, rag)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary_cls,secondary_cls", [(GeminiClient, DeepSeekClient), (DeepSeekClient, GeminiClient)]
)
async def test_fallback_history_matches_destination_format(
    primary_cls, secondary_cls, valid_settings_dict
):
    history = ChatHistoryService()
    await history.add_user_message(42, "VPN не работает")
    await history.add_assistant_message(42, "Какая ОС?")
    rag = MagicMock(build_faq_context=AsyncMock(return_value=FaqContext.EMPTY))
    settings = Settings(**valid_settings_dict)
    router = MagicMock()
    router.list_tools.return_value = []
    primary = primary_cls(settings, router, history, rag)
    secondary = secondary_cls(settings, router, history, rag)
    primary.call_api = AsyncMock(
        side_effect=LlmProcessingException("rate limited", status_code=429)
    )
    response = (
        {"candidates": [{"content": {"parts": [{"text": "Готово"}]}}]}
        if secondary_cls is GeminiClient
        else {"choices": [{"message": {"content": "Готово"}}]}
    )
    secondary.call_api = AsyncMock(return_value=response)
    await LlmFallbackClient([primary, secondary]).chat("Android", 42)
    payload = secondary.call_api.await_args.args[0]
    expected_key = "parts" if secondary_cls is GeminiClient else "content"
    assert all(expected_key in message for message in payload), payload
    assert len(await history.get_history(42)) == 4
    rag.build_faq_context.assert_awaited_once_with("VPN не работает Android", set())


@pytest.mark.asyncio
async def test_second_rejection_keeps_original_problem():
    history = ChatHistoryService()
    rag = MagicMock(build_faq_context=AsyncMock(return_value=FaqContext.EMPTY))
    instance = client(DeepSeekClient, history, rag)
    for message in ["Все серверы n/a, VPN не работает", "Не помогло, у меня Android"]:
        await instance.prepare_turn(message, 42)
        await instance.persist_success(42, message, LlmReply(text="Попробуйте ещё раз"))
    await instance.prepare_turn("Всё ещё не работает", 42)
    query = rag.build_faq_context.await_args.args[0]
    assert query == (
        "Все серверы n/a, VPN не работает Не помогло, у меня Android Всё ещё не работает"
    )


@pytest.mark.asyncio
async def test_followups_after_restart_preserve_facts_and_stop_at_new_topic():
    history = ChatHistoryService()
    for message in [
        "Все серверы n/a",
        "Не помогло, у меня Android",
        "Как оплатить подписку?",
        "Не помогло, карта Visa",
    ]:
        await history.add_user_message(42, message)
        await history.add_assistant_message(42, "Попробуйте ещё раз")
    saved_history = await history.get_history(42)
    cold_history = ChatHistoryService()

    async def restore(user_id):
        for message in saved_history:
            await cold_history._append(user_id, message["role"], message["content"])

    cold_history._load_from_database = restore
    rag = MagicMock(build_faq_context=AsyncMock(return_value=FaqContext.EMPTY))
    instance = client(DeepSeekClient, cold_history, rag)
    await instance.prepare_turn("Всё ещё не работает", 42)
    query = rag.build_faq_context.await_args.args[0]
    assert query == "Как оплатить подписку? Не помогло, карта Visa Всё ещё не работает"


@pytest.mark.asyncio
async def test_explicit_attribution_excludes_only_used_candidate_and_is_cleared():
    history = ChatHistoryService()
    context = FaqContext(
        text="candidates",
        results=[FaqResult("A", "Шаг A", 0.9, 0.04), FaqResult("B", "Шаг B", 0.8, 0.03)],
        max_similarity=0.9,
        best_question="A",
    )
    history.record_faq_context(42, context, used_questions=["B", "unknown"])
    history.reject_last_faq(42)
    assert history.get_rejected_faq_questions(42) == {"B"}
    history.clear_rejected_faqs_if_new_topic(42, "Оплата")
    history.record_faq_context(42, context)
    history.reject_last_faq(42)
    assert history.get_rejected_faq_questions(42) == set()


@pytest.mark.asyncio
async def test_rejection_does_not_exclude_unused_primary_candidate():
    history = ChatHistoryService()
    context = FaqContext(
        text="candidates",
        results=[FaqResult("A", "Шаг A", 0.9, 0.04), FaqResult("B", "Шаг B", 0.8, 0.03)],
        max_similarity=0.9,
        best_question="A",
    )
    rag = MagicMock(build_faq_context=AsyncMock(return_value=context))
    instance = client(DeepSeekClient, history, rag)
    await instance.persist_success(
        42, "VPN не работает", LlmReply(text="Шаг B", faq_context=context)
    )
    await instance.prepare_turn("Не помогло", 42)
    excluded = rag.build_faq_context.await_args.args[1]
    assert "A" not in excluded, excluded


class NewEmbeddingProvider:
    model = "new-model"

    def get_dimension(self):
        return 4

    async def embed_batch(self, texts):
        return [[] for _ in texts]

    async def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def indexed_service():
    db = MagicMock()
    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)
    db.session.return_value.__aenter__.return_value = session
    service = FaqEmbeddingService(db, NewEmbeddingProvider())
    service.init_schema = AsyncMock()
    service.get_faq_index_fingerprint = AsyncMock(return_value="old-model-fingerprint")
    service.get_faq_count = AsyncMock(return_value=1)
    service.get_indexed_faq_count = AsyncMock(return_value=1)
    service.update_faq_index_fingerprint = AsyncMock()
    return service, session


@pytest.mark.asyncio
async def test_failed_model_migration_does_not_query_old_vectors(tmp_path):
    source = tmp_path / "faq.json"
    source.write_text(json.dumps([{"question": "Q", "answer": "A"}]))
    service, session = indexed_service()
    await FaqInitializer(service, source).run()
    await service.search("оплата")
    sql = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("ts_rank" in query for query in sql)
    assert not any("<=>" in query for query in sql), "new-model query searched old-model vectors"
    assert service.embedding_cache == {}


@pytest.mark.asyncio
async def test_empty_source_removes_old_faq_before_marking_current(tmp_path):
    source = tmp_path / "faq.json"
    source.write_text("[]")
    service, session = indexed_service()
    await FaqInitializer(service, source).run()
    service.update_faq_index_fingerprint.assert_awaited_once()
    sql = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("DELETE FROM faq" in query for query in sql), (
        "new fingerprint saved but old FAQ remains"
    )


@pytest.mark.asyncio
async def test_successful_retry_reenables_vector_search(tmp_path):
    source = tmp_path / "faq.json"
    source.write_text(json.dumps([{"question": "Q", "answer": "A"}]))
    service, session = indexed_service()
    initializer = FaqInitializer(service, source)
    await initializer.run()
    assert service.vector_search_enabled is False
    service.embedding_provider.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    await initializer.run()
    service.update_faq_index_fingerprint.assert_awaited_once()
    session.execute.reset_mock()
    await service.search("оплата")
    assert any("<=>" in str(call.args[0]) for call in session.execute.await_args_list)


@pytest.mark.asyncio
async def test_matching_complete_index_enables_vector_search_without_replacement(tmp_path):
    source = tmp_path / "faq.json"
    source.write_text(json.dumps([{"question": "Q", "answer": "A"}]))
    service, session = indexed_service()
    initializer = FaqInitializer(service, source)
    service.get_faq_index_fingerprint.return_value = service.get_index_fingerprint(
        initializer.compute_hash(source)
    )
    await initializer.run()
    session.execute.assert_not_awaited()
    assert service.vector_search_enabled is True
    await service.search("оплата")
    assert any("<=>" in str(call.args[0]) for call in session.execute.await_args_list)


@pytest.mark.asyncio
async def test_failed_empty_replacement_does_not_update_fingerprint(tmp_path):
    source = tmp_path / "faq.json"
    source.write_text("[]")
    service, session = indexed_service()
    session.execute.side_effect = RuntimeError("database unavailable")
    await FaqInitializer(service, source).run()
    service.update_faq_index_fingerprint.assert_not_awaited()
    assert service.vector_search_enabled is False
