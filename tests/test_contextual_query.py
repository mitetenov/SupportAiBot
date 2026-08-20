"""Tests for contextual search query rewriting in AbstractLlmClient."""

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.llm.deepseek import DeepSeekClient
from app.llm.mcp_router import McpRouter
from app.rag.service import FaqEmbeddingService
from app.storage.chat_history import ChatHistoryService

USER_ID = 1
PREVIOUS = "не работает впн на телефоне"


@pytest.fixture
def client() -> DeepSeekClient:
    settings = Settings(
        telegram_bot_token="test_token",
        telegram_support_group_chat_id=-1001234567890,
        deepseek_api_key="sk-test",
        deepseek_model="deepseek-chat",
        deepseek_base_url="http://localhost:9999",
        remnawave_mcp_url="http://localhost:3100",
    )
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.list_tools.return_value = []
    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_last_user_message.return_value = PREVIOUS
    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)

    return DeepSeekClient(
        settings=settings,
        mcp_router=mcp_router,
        chat_history_service=chat_history_service,
        faq_embedding_service=faq_embedding_service,
    )


class TestContextualSearchQuery:
    def test_prefix_context_for_continuation_particle(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "а на айфоне?") == f"{PREVIOUS} а на айфоне?"
        assert client.build_contextual_search_query(USER_ID, "и на компе?") == f"{PREVIOUS} и на компе?"
        assert client.build_contextual_search_query(USER_ID, "но у меня андроид") == f"{PREVIOUS} но у меня андроид"
        assert client.build_contextual_search_query(USER_ID, "ну так что?") == f"{PREVIOUS} ну так что?"

    def test_prefix_context_for_bare_platform_name(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "айфон") == f"{PREVIOUS} айфон"
        assert client.build_contextual_search_query(USER_ID, "iphone") == f"{PREVIOUS} iphone"
        assert client.build_contextual_search_query(USER_ID, "андроид") == f"{PREVIOUS} андроид"
        assert client.build_contextual_search_query(USER_ID, "android") == f"{PREVIOUS} android"
        assert client.build_contextual_search_query(USER_ID, "винда") == f"{PREVIOUS} винда"
        assert client.build_contextual_search_query(USER_ID, "windows") == f"{PREVIOUS} windows"
        assert client.build_contextual_search_query(USER_ID, "макбук") == f"{PREVIOUS} макбук"
        assert client.build_contextual_search_query(USER_ID, "мак") == f"{PREVIOUS} мак"
        assert client.build_contextual_search_query(USER_ID, "mac") == f"{PREVIOUS} mac"
        assert client.build_contextual_search_query(USER_ID, "linux") == f"{PREVIOUS} linux"
        assert client.build_contextual_search_query(USER_ID, "tv") == f"{PREVIOUS} tv"
        assert client.build_contextual_search_query(USER_ID, "телевизор") == f"{PREVIOUS} телевизор"

    def test_prefix_context_for_anaphoric_reference(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "это не помогло") == f"{PREVIOUS} это не помогло"
        assert client.build_contextual_search_query(USER_ID, "этот вариант") == f"{PREVIOUS} этот вариант"
        assert client.build_contextual_search_query(USER_ID, "эта ошибка") == f"{PREVIOUS} эта ошибка"
        assert client.build_contextual_search_query(USER_ID, "туда не заходит") == f"{PREVIOUS} туда не заходит"
        assert client.build_contextual_search_query(USER_ID, "там ошибка") == f"{PREVIOUS} там ошибка"
        assert client.build_contextual_search_query(USER_ID, "тут не работает") == f"{PREVIOUS} тут не работает"
        assert client.build_contextual_search_query(USER_ID, "оно не грузит") == f"{PREVIOUS} оно не грузит"
        assert client.build_contextual_search_query(USER_ID, "его нет") == f"{PREVIOUS} его нет"
        assert client.build_contextual_search_query(USER_ID, "её не видно") == f"{PREVIOUS} её не видно"
        assert client.build_contextual_search_query(USER_ID, "в нём ошибка") == f"{PREVIOUS} в нём ошибка"
        assert client.build_contextual_search_query(USER_ID, "с ним не работает") == f"{PREVIOUS} с ним не работает"
        assert client.build_contextual_search_query(USER_ID, "такое бывает?") == f"{PREVIOUS} такое бывает?"

    def test_prefix_context_for_bare_acknowledgement(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "да") == f"{PREVIOUS} да"
        assert client.build_contextual_search_query(USER_ID, "нет") == f"{PREVIOUS} нет"
        assert client.build_contextual_search_query(USER_ID, "ага") == f"{PREVIOUS} ага"
        assert client.build_contextual_search_query(USER_ID, "угу") == f"{PREVIOUS} угу"
        assert client.build_contextual_search_query(USER_ID, "ок") == f"{PREVIOUS} ок"
        assert client.build_contextual_search_query(USER_ID, "окей") == f"{PREVIOUS} окей"
        assert client.build_contextual_search_query(USER_ID, "понял") == f"{PREVIOUS} понял"
        assert client.build_contextual_search_query(USER_ID, "поняла") == f"{PREVIOUS} поняла"

    def test_prefix_context_for_message_with_no_letters(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "???") == f"{PREVIOUS} ???"
        assert client.build_contextual_search_query(USER_ID, "🤔") == f"{PREVIOUS} 🤔"
        assert client.build_contextual_search_query(USER_ID, "123") == f"{PREVIOUS} 123"

    def test_not_prefix_context_for_short_self_contained_question(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "Как оплатить?") == "Как оплатить?"

    def test_not_prefix_context_for_other_short_topic_changes(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "Где мой QR-код") == "Где мой QR-код"
        assert client.build_contextual_search_query(USER_ID, "Сколько стоит?") == "Сколько стоит?"
        assert client.build_contextual_search_query(USER_ID, "Верните деньги") == "Верните деньги"

    def test_not_prefix_context_for_long_message(self, client: DeepSeekClient):
        msg = "У меня перестал работать интернет после обновления приложения, и я не понимаю, что делать дальше"
        assert client.build_contextual_search_query(USER_ID, msg) == msg

    def test_return_message_when_no_history(self, client: DeepSeekClient):
        client.chat_history_service.get_last_user_message.return_value = None
        assert client.build_contextual_search_query(USER_ID, "айфон") == "айфон"

    def test_not_duplicate_identical_repeated_message(self, client: DeepSeekClient):
        client.chat_history_service.get_last_user_message.return_value = "айфон"
        assert client.build_contextual_search_query(USER_ID, "айфон") == "айфон"

    def test_passthrough_empty_input(self, client: DeepSeekClient):
        assert client.build_contextual_search_query(USER_ID, "") == ""
        assert client.build_contextual_search_query(USER_ID, None) is None
