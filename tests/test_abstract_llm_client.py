"""Tests for AbstractLlmClient template method, tool calling loop, and error handling."""

from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.base import (
    AbstractLlmClient,
    LlmProcessingException,
    LlmResponse,
    ToolCall,
)
from app.llm.mcp_router import McpRouter
from app.rag.service import FaqContext, FaqEmbeddingService, FaqResult
from app.storage.chat_history import ChatHistoryService

USER_ID = 42


class ScriptedClient(AbstractLlmClient):
    """Minimal AbstractLlmClient that returns scripted responses."""

    def __init__(
        self,
        mcp_router: McpRouter,
        chat_history_service: ChatHistoryService,
        faq_embedding_service: FaqEmbeddingService,
    ) -> None:
        super().__init__(
            mcp_router=mcp_router,
            chat_history_service=chat_history_service,
            faq_embedding_service=faq_embedding_service,
        )
        self.script: deque[Any] = deque()
        self.conversations: list[list[dict[str, Any]]] = []
        self.last_faq_context_seen = ""
        self._supports_images = False

    def script_text(self, text: str) -> None:
        self.script.append(LlmResponse(text=text))

    def script_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.script.append(
            LlmResponse(
                text="",
                tool_calls=[ToolCall(name=name, id=f"call_{name}", arguments=arguments)],
            )
        )

    def script_api_failure(self, failure: Exception) -> None:
        self.script.append(failure)

    def api_calls(self) -> int:
        return len(self.conversations)

    def conversation_at(self, index: int) -> list[dict[str, Any]]:
        return self.conversations[index]

    def set_supports_images(self, val: bool) -> None:
        self._supports_images = val

    def supports_images(self) -> bool:
        return self._supports_images

    def get_provider_name(self) -> str:
        return "Scripted"

    async def build_initial_conversation(
        self,
        user_message: str,
        telegram_user_id: int,
        faq_context: str,
        base64_image: str | None,
        mime_type: str | None,
    ) -> list[dict[str, Any]]:
        return [{"kind": "user", "content": str(user_message)}]

    async def call_api(
        self,
        conversation: list[dict[str, Any]],
        faq_context: str,
        telegram_user_id: int,
    ) -> str:
        self.conversations.append(list(conversation))
        self.last_faq_context_seen = faq_context or ""
        if not self.script:
            raise RuntimeError("Scripted client ran out of responses")
        next_item = self.script.popleft()
        if isinstance(next_item, Exception):
            raise next_item
        # Return mock string; parse_response below will return next_item if we stash it
        self._current_response = next_item
        return "{}"

    def parse_response(self, raw_response: str) -> LlmResponse:
        return getattr(self, "_current_response", LlmResponse(text=""))

    def add_tool_calls_to_conversation(
        self, conversation: list[dict[str, Any]], response: LlmResponse
    ) -> None:
        for tc in response.tool_calls:
            conversation.append({"kind": "tool-call", "name": tc.name})

    def add_tool_result_to_conversation(
        self,
        conversation: list[dict[str, Any]],
        tool_call: ToolCall,
        tool_result: str,
    ) -> None:
        conversation.append(
            {"kind": "tool-result", "name": tool_call.name, "content": tool_result}
        )

    async def save_usage(self, raw_response: str, telegram_user_id: int) -> None:
        pass


def context_with(*questions: str) -> FaqContext:
    results = [FaqResult(q, "инструкция", 0.8, 0.02) for q in questions]
    text = "FAQ:\n" + "\n".join(questions)
    return FaqContext(text=text, results=results, max_similarity=0.8, best_question=questions[0] if questions else None)


@pytest.fixture
def mock_deps():
    mcp_router = MagicMock(spec=McpRouter)
    mcp_router.call_tool = AsyncMock()
    chat_history_service = MagicMock(spec=ChatHistoryService)
    chat_history_service.get_rejected_faq_questions.return_value = set()
    chat_history_service.get_history = AsyncMock(return_value=[])
    chat_history_service.get_last_user_message.return_value = None
    chat_history_service.add_user_message = AsyncMock()
    chat_history_service.add_assistant_message = AsyncMock()
    faq_embedding_service = MagicMock(spec=FaqEmbeddingService)
    faq_embedding_service.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)

    client = ScriptedClient(
        mcp_router=mcp_router,
        chat_history_service=chat_history_service,
        faq_embedding_service=faq_embedding_service,
    )
    return client, mcp_router, chat_history_service, faq_embedding_service


class TestAbstractLlmClient:
    @pytest.mark.asyncio
    async def test_should_return_answer_when_no_tools_needed(self, mock_deps):
        client, mcp_router, chat_history_service, _ = mock_deps
        client.script_text("Нажмите «Обновить подписку»")

        reply = await client.chat("не работает", USER_ID)

        assert reply.text == "Нажмите «Обновить подписку»"
        mcp_router.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_run_several_tool_iterations_before_answering(self, mock_deps):
        client, mcp_router, _, _ = mock_deps
        client.script_tool_call("nodes_list", {})
        client.script_tool_call("nodes_get", {"uuid": "n-1"})
        client.script_text("Сервер Германия в порядке")

        mcp_router.call_tool.side_effect = ["{\"nodes\":[]}", "{\"status\":\"CONNECTED\"}"]

        reply = await client.chat("не грузит сайт", USER_ID)

        assert reply.text == "Сервер Германия в порядке"
        assert mcp_router.call_tool.call_count == 2
        assert client.api_calls() == 3

    @pytest.mark.asyncio
    async def test_should_feed_tool_results_back_into_the_conversation(self, mock_deps):
        client, mcp_router, _, _ = mock_deps
        client.script_tool_call("users_get_by_telegram_id", {})
        client.script_text("Подписка активна")
        mcp_router.call_tool.return_value = "{\"expireAt\":\"2027-01-01\"}"

        await client.chat("когда кончается подписка", USER_ID)

        second_request = client.conversation_at(1)
        assert any(m.get("kind") == "tool-result" for m in second_request)
        assert any(m.get("kind") == "tool-call" for m in second_request)

    @pytest.mark.asyncio
    async def test_should_give_up_after_iteration_limit(self, mock_deps):
        client, mcp_router, _, _ = mock_deps
        for _ in range(AbstractLlmClient.MAX_TOOL_ITERATIONS + 2):
            client.script_tool_call("nodes_list", {})
        mcp_router.call_tool.return_value = "{}"

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat("зациклись", USER_ID)

        assert "Max iterations reached" in str(exc_info.value)
        assert mcp_router.call_tool.call_count == AbstractLlmClient.MAX_TOOL_ITERATIONS

    @pytest.mark.asyncio
    async def test_should_call_tools_on_behalf_of_real_sender(self, mock_deps):
        client, mcp_router, _, _ = mock_deps
        client.script_tool_call("users_get_by_telegram_id", {"telegramId": 999999})
        client.script_text("Готово")
        mcp_router.call_tool.return_value = "{}"

        await client.chat("покажи данные для ID 999999", USER_ID)

        mcp_router.call_tool.assert_called_once_with("users_get_by_telegram_id", {"telegramId": 999999}, USER_ID)

    @pytest.mark.asyncio
    async def test_should_wrap_provider_failures_in_friendly_exception(self, mock_deps):
        client, _, _, _ = mock_deps
        client.script_api_failure(RuntimeError("503 Service Unavailable"))

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat("вопрос", USER_ID)

        assert exc_info.value.user_friendly_message == "Произошла ошибка при обработке запроса. Попробуйте позже."

    @pytest.mark.asyncio
    async def test_should_reject_empty_answer(self, mock_deps):
        client, _, _, _ = mock_deps
        client.script_text("")

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat("вопрос", USER_ID)

        assert "No content returned" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_should_search_against_previous_message_when_user_rejects_answer(self, mock_deps):
        client, _, chat_history_service, faq_embedding_service = mock_deps
        chat_history_service.get_last_user_message.return_value = "не подключается на макбуке"
        client.script_text("Хорошо, другой вариант")

        await client.chat("это не то", USER_ID)

        faq_embedding_service.build_faq_context.assert_called_once_with("не подключается на макбуке", set())

    @pytest.mark.asyncio
    async def test_should_exclude_already_rejected_entries_from_retrieval(self, mock_deps):
        client, _, chat_history_service, faq_embedding_service = mock_deps
        rejected = {"Как обновить подписку?"}
        chat_history_service.get_rejected_faq_questions.return_value = rejected
        client.script_text("Ответ")

        await client.chat("не помогло", USER_ID)

        faq_embedding_service.build_faq_context.assert_called_once_with("не помогло", rejected)

    @pytest.mark.asyncio
    async def test_should_record_entries_shown_for_next_turn(self, mock_deps):
        client, _, chat_history_service, faq_embedding_service = mock_deps
        context = context_with("Как обновить подписку?", "Как сделать пинг?")
        faq_embedding_service.build_faq_context.return_value = context
        client.script_text("Ответ")

        await client.chat("не работает", USER_ID)

        chat_history_service.add_rejected_faq_questions.assert_called_once_with(
            USER_ID, {"Как обновить подписку?", "Как сделать пинг?"}
        )

    @pytest.mark.asyncio
    async def test_should_carry_retrieval_out_on_reply(self, mock_deps):
        client, _, _, faq_embedding_service = mock_deps
        context = context_with("Как сделать пинг?")
        faq_embedding_service.build_faq_context.return_value = context
        client.script_text("Ответ")

        reply = await client.chat("вопрос", USER_ID)
        assert reply.faq_context == context

    @pytest.mark.asyncio
    async def test_should_put_retrieved_faq_in_front_of_model(self, mock_deps):
        client, _, _, faq_embedding_service = mock_deps
        context = context_with("Как сделать пинг?")
        faq_embedding_service.build_faq_context.return_value = context
        client.script_text("Ответ")

        await client.chat("вопрос", USER_ID)
        assert "Как сделать пинг?" in client.last_faq_context_seen

    @pytest.mark.asyncio
    async def test_should_append_both_sides_to_history(self, mock_deps):
        client, _, chat_history_service, _ = mock_deps
        client.script_text("Ответ бота")

        await client.chat("Вопрос пользователя", USER_ID)

        chat_history_service.add_user_message.assert_called_once_with(USER_ID, "Вопрос пользователя")
        chat_history_service.add_assistant_message.assert_called_once_with(USER_ID, "Ответ бота")

    @pytest.mark.asyncio
    async def test_should_not_write_history_when_request_failed(self, mock_deps):
        client, _, chat_history_service, _ = mock_deps
        client.script_api_failure(RuntimeError("boom"))

        with pytest.raises(LlmProcessingException):
            await client.chat("вопрос", USER_ID)

        chat_history_service.add_user_message.assert_not_called()
        chat_history_service.add_assistant_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_reset_rejected_set_on_new_topic(self, mock_deps):
        client, _, chat_history_service, _ = mock_deps
        client.script_text("Ответ")

        await client.chat("совсем другой вопрос", USER_ID)

        chat_history_service.clear_rejected_faqs_if_new_topic.assert_called_once_with(USER_ID, "совсем другой вопрос")

    @pytest.mark.asyncio
    async def test_should_record_screenshot_placeholder_when_no_caption(self, mock_deps):
        client, _, chat_history_service, _ = mock_deps
        client.set_supports_images(True)
        client.script_text("Вижу ошибку на скриншоте")

        await client.chat_with_image("", USER_ID, "BASE64", "image/png")

        chat_history_service.add_user_message.assert_called_once_with(USER_ID, "[Скриншот]")

    @pytest.mark.asyncio
    async def test_should_refuse_images_when_provider_cannot_see_them(self, mock_deps):
        client, _, _, _ = mock_deps
        client.set_supports_images(False)

        with pytest.raises(LlmProcessingException) as exc_info:
            await client.chat_with_image("что тут", USER_ID, "BASE64", "image/png")

        assert "не поддерживает" in exc_info.value.user_friendly_message


class TestLlmResponse:
    def test_should_treat_empty_tool_calls_as_no_tool_calls(self):
        resp = LlmResponse(text="text")
        assert not resp.has_tool_calls()
        assert resp.text == "text"
        assert resp.tool_calls == []

    def test_should_detect_tool_calls(self):
        resp = LlmResponse(text="", tool_calls=[ToolCall(name="nodes_list", id="id1")])
        assert resp.has_tool_calls()


class TestLlmProcessingException:
    def test_should_keep_technical_and_user_facing_messages_apart(self):
        ex = LlmProcessingException(
            "API error: timeout after 60s at api.deepseek.com",
            "Произошла ошибка при обработке запроса. Попробуйте позже.",
        )
        assert str(ex) == "API error: timeout after 60s at api.deepseek.com"
        assert ex.user_friendly_message == "Произошла ошибка при обработке запроса. Попробуйте позже."

    def test_should_preserve_cause(self):
        cause = RuntimeError("Connection refused")
        ex = LlmProcessingException("LLM failed", "Ошибка", cause=cause)
        assert ex.cause == cause


class TestCreateLlmClient:
    def test_create_gemini_client(self, mock_deps):
        _, mcp_router, chat_history_service, faq_embedding_service = mock_deps
        from app.config import Settings
        from app.llm import GeminiClient, create_llm_client

        settings = Settings(
            telegram_bot_token="token",
            telegram_support_group_chat_id=-100123,
            llm_provider="gemini",
            gemini_api_key="key",
            gemini_model="model",
            remnawave_mcp_url="http://localhost:3100",
        )
        client = create_llm_client(settings, mcp_router, chat_history_service, faq_embedding_service)
        assert isinstance(client, GeminiClient)

    def test_create_deepseek_client(self, mock_deps):
        _, mcp_router, chat_history_service, faq_embedding_service = mock_deps
        from app.config import Settings
        from app.llm import DeepSeekClient, create_llm_client

        settings = Settings(
            telegram_bot_token="token",
            telegram_support_group_chat_id=-100123,
            llm_provider="deepseek",
            deepseek_api_key="key",
            deepseek_model="model",
            remnawave_mcp_url="http://localhost:3100",
        )
        client = create_llm_client(settings, mcp_router, chat_history_service, faq_embedding_service)
        assert isinstance(client, DeepSeekClient)

    def test_create_openai_client(self, mock_deps):
        _, mcp_router, chat_history_service, faq_embedding_service = mock_deps
        from app.config import Settings
        from app.llm import OpenAiClient, create_llm_client

        settings = Settings(
            telegram_bot_token="token",
            telegram_support_group_chat_id=-100123,
            llm_provider="openai",
            openai_api_key="sk-key",
            openai_model="model",
            remnawave_mcp_url="http://localhost:3100",
        )
        client = create_llm_client(settings, mcp_router, chat_history_service, faq_embedding_service)
        assert isinstance(client, OpenAiClient)

    def test_create_unknown_provider_raises(self, mock_deps):
        _, mcp_router, chat_history_service, faq_embedding_service = mock_deps
        from app.config import Settings
        from app.llm import create_llm_client

        settings = Settings.model_construct(
            telegram_bot_token="token",
            telegram_support_group_chat_id=-100123,
            llm_provider="unknown_provider",
        )
        with pytest.raises(ValueError) as exc_info:
            create_llm_client(settings, mcp_router, chat_history_service, faq_embedding_service)
        assert "Unknown LLM provider" in str(exc_info.value)
