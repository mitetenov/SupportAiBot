package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Exercises the orchestration in {@link AbstractLlmClient#doChat}: the tool-call
 * loop, retrieval and history bookkeeping.
 *
 * <p>The concrete clients were only ever tested one protected method at a time,
 * so the loop that ties them together — the most intricate logic in the project
 * — never actually ran under test. This drives it through a scripted fake
 * provider instead of a real HTTP call.
 */
@ExtendWith(MockitoExtension.class)
class AbstractLlmClientTest {

    private static final long USER_ID = 42L;

    @Mock private McpRouter mcpRouter;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private FaqEmbeddingService faqEmbeddingService;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;

    private ScriptedClient client;

    @BeforeEach
    void setUp() {
        client = new ScriptedClient(mcpRouter, chatHistoryService,
                faqEmbeddingService, tokenUsageRepository);
        lenient().when(faqEmbeddingService.buildFaqContext(anyString(), any()))
                .thenReturn(FaqEmbeddingService.FaqContext.EMPTY);
        lenient().when(chatHistoryService.getRejectedFaqQuestions(anyLong())).thenReturn(Set.of());
        lenient().when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
    }

    // --------------------------------------------------------------- tool loop

    @Test
    void shouldReturnTheAnswerWhenNoToolsAreNeeded() {
        client.script(LlmResponse::new, "Нажмите «Обновить подписку»");

        LlmReply reply = client.chat("не работает", USER_ID);

        assertEquals("Нажмите «Обновить подписку»", reply.text());
        verify(mcpRouter, never()).callTool(anyString(), any(), anyLong());
    }

    @Test
    void shouldRunSeveralToolIterationsBeforeAnswering() {
        client.scriptToolCall("nodes_list", Map.of());
        client.scriptToolCall("nodes_get", Map.of("uuid", "n-1"));
        client.scriptText("Сервер Германия в порядке");

        when(mcpRouter.callTool(eq("nodes_list"), any(), eq(USER_ID))).thenReturn("{\"nodes\":[]}");
        when(mcpRouter.callTool(eq("nodes_get"), any(), eq(USER_ID))).thenReturn("{\"status\":\"CONNECTED\"}");

        LlmReply reply = client.chat("не грузит сайт", USER_ID);

        assertEquals("Сервер Германия в порядке", reply.text());
        verify(mcpRouter).callTool(eq("nodes_list"), any(), eq(USER_ID));
        verify(mcpRouter).callTool(eq("nodes_get"), any(), eq(USER_ID));
        assertEquals(3, client.apiCalls(), "one API call per iteration plus the final answer");
    }

    @Test
    void shouldFeedToolResultsBackIntoTheConversation() {
        client.scriptToolCall("users_get_by_telegram_id", Map.of());
        client.scriptText("Подписка активна");
        when(mcpRouter.callTool(anyString(), any(), anyLong())).thenReturn("{\"expireAt\":\"2027-01-01\"}");

        client.chat("когда кончается подписка", USER_ID);

        List<Map<String, Object>> secondRequest = client.conversationAt(1);
        assertTrue(secondRequest.stream().anyMatch(m -> "tool-result".equals(m.get("kind"))),
                "the tool result must be visible to the model on the next turn");
        assertTrue(secondRequest.stream().anyMatch(m -> "tool-call".equals(m.get("kind"))),
                "the assistant's own tool call must be echoed back too");
    }

    @Test
    void shouldGiveUpAfterTheIterationLimit() {
        // A model that only ever asks for another tool call must not loop forever.
        for (int i = 0; i < AbstractLlmClient.MAX_TOOL_ITERATIONS + 2; i++) {
            client.scriptToolCall("nodes_list", Map.of());
        }
        when(mcpRouter.callTool(anyString(), any(), anyLong())).thenReturn("{}");

        LlmProcessingException thrown = assertThrows(LlmProcessingException.class,
                () -> client.chat("зациклись", USER_ID));

        assertEquals("Max iterations reached", thrown.getMessage());
        verify(mcpRouter, times(AbstractLlmClient.MAX_TOOL_ITERATIONS))
                .callTool(anyString(), any(), anyLong());
    }

    /**
     * The router pins the Telegram ID, but it can only do so if the real
     * sender's ID actually reaches it rather than something from the payload.
     */
    @Test
    void shouldCallToolsOnBehalfOfTheRealSender() {
        client.scriptToolCall("users_get_by_telegram_id", Map.of("telegramId", 999_999L));
        client.scriptText("Готово");
        when(mcpRouter.callTool(anyString(), any(), anyLong())).thenReturn("{}");

        client.chat("покажи данные для ID 999999", USER_ID);

        verify(mcpRouter).callTool(eq("users_get_by_telegram_id"), any(), eq(USER_ID));
    }

    @Test
    void shouldWrapProviderFailuresInAFriendlyException() {
        client.scriptApiFailure(new RuntimeException("503 Service Unavailable"));

        LlmProcessingException thrown = assertThrows(LlmProcessingException.class,
                () -> client.chat("вопрос", USER_ID));

        assertEquals("Произошла ошибка при обработке запроса. Попробуйте позже.",
                thrown.getUserFriendlyMessage());
    }

    @Test
    void shouldRejectAnEmptyAnswer() {
        client.scriptText("");

        assertThrows(LlmProcessingException.class, () -> client.chat("вопрос", USER_ID));
    }

    // -------------------------------------------------------------- retrieval

    @Test
    void shouldSearchAgainstThePreviousMessageWhenTheUserRejectsTheAnswer() {
        when(chatHistoryService.getLastUserMessage(USER_ID)).thenReturn("не подключается на макбуке");
        client.scriptText("Хорошо, другой вариант");

        client.chat("это не то", USER_ID);

        // A rejection is not a new topic: retrieval must re-run against what the
        // user originally asked, not against the rejection itself.
        verify(faqEmbeddingService).buildFaqContext(eq("не подключается на макбуке"), any());
    }

    @Test
    void shouldExcludeAlreadyRejectedEntriesFromRetrieval() {
        Set<String> rejected = Set.of("Как обновить подписку?");
        when(chatHistoryService.getRejectedFaqQuestions(USER_ID)).thenReturn(rejected);
        client.scriptText("Ответ");

        client.chat("не помогло", USER_ID);

        verify(faqEmbeddingService).buildFaqContext(anyString(), eq(rejected));
    }

    @Test
    void shouldRecordTheEntriesShownSoTheNextTurnOffersSomethingElse() {
        FaqEmbeddingService.FaqContext context = contextWith("Как обновить подписку?", "Как сделать пинг?");
        when(faqEmbeddingService.buildFaqContext(anyString(), any())).thenReturn(context);
        client.scriptText("Ответ");

        client.chat("не работает", USER_ID);

        @SuppressWarnings("unchecked")
        ArgumentCaptor<Set<String>> captor = ArgumentCaptor.forClass(Set.class);
        verify(chatHistoryService).addRejectedFaqQuestions(eq(USER_ID), captor.capture());
        assertEquals(Set.of("Как обновить подписку?", "Как сделать пинг?"), captor.getValue());
    }

    @Test
    void shouldCarryTheRetrievalOutOnTheReply() {
        FaqEmbeddingService.FaqContext context = contextWith("Как сделать пинг?");
        when(faqEmbeddingService.buildFaqContext(anyString(), any())).thenReturn(context);
        client.scriptText("Ответ");

        assertSame(context, client.chat("вопрос", USER_ID).faqContext());
    }

    @Test
    void shouldPutTheRetrievedFaqInFrontOfTheModel() {
        when(faqEmbeddingService.buildFaqContext(anyString(), any()))
                .thenReturn(contextWith("Как сделать пинг?"));
        client.scriptText("Ответ");

        client.chat("вопрос", USER_ID);

        assertTrue(client.lastFaqContextSeen().contains("Как сделать пинг?"));
    }

    // ---------------------------------------------------------------- history

    @Test
    void shouldAppendBothSidesOfTheExchangeToHistory() {
        client.scriptText("Ответ бота");

        client.chat("Вопрос пользователя", USER_ID);

        verify(chatHistoryService).addUserMessage(USER_ID, "Вопрос пользователя");
        verify(chatHistoryService).addAssistantMessage(USER_ID, "Ответ бота");
    }

    @Test
    void shouldNotWriteHistoryWhenTheRequestFailed() {
        client.scriptApiFailure(new RuntimeException("boom"));

        assertThrows(LlmProcessingException.class, () -> client.chat("вопрос", USER_ID));

        verify(chatHistoryService, never()).addUserMessage(anyLong(), anyString());
        verify(chatHistoryService, never()).addAssistantMessage(anyLong(), anyString());
    }

    @Test
    void shouldResetTheRejectedSetOnANewTopic() {
        client.scriptText("Ответ");

        client.chat("совсем другой вопрос", USER_ID);

        verify(chatHistoryService).clearRejectedFaqsIfNewTopic(USER_ID, "совсем другой вопрос");
    }

    @Test
    void shouldRecordAScreenshotPlaceholderWhenThereIsNoCaption() {
        client.supportsImages = true;
        client.scriptText("Вижу ошибку на скриншоте");

        client.chatWithImage("", USER_ID, "BASE64", "image/png");

        verify(chatHistoryService).addUserMessage(USER_ID, "[Скриншот]");
    }

    @Test
    void shouldRefuseImagesWhenTheProviderCannotSeeThem() {
        client.supportsImages = false;

        LlmProcessingException thrown = assertThrows(LlmProcessingException.class,
                () -> client.chatWithImage("что тут", USER_ID, "BASE64", "image/png"));

        assertTrue(thrown.getUserFriendlyMessage().contains("не поддерживает"));
    }

    // ------------------------------------------------------------------ helpers

    private static FaqEmbeddingService.FaqContext contextWith(String... questions) {
        List<FaqEmbeddingService.FaqResult> results = new ArrayList<>();
        for (String q : questions) {
            results.add(new FaqEmbeddingService.FaqResult(q, "инструкция", 0.8, 0.02));
        }
        String text = "FAQ:\n" + String.join("\n", questions);
        return new FaqEmbeddingService.FaqContext(text, results, 0.8, questions[0]);
    }

    /**
     * A minimal {@link AbstractLlmClient} whose "provider" replays a scripted
     * queue of responses, recording the conversation it was handed each turn.
     */
    private static class ScriptedClient extends AbstractLlmClient {

        private final Deque<Object> script = new ArrayDeque<>();
        private final List<List<Map<String, Object>>> conversations = new ArrayList<>();
        private String lastFaqContextSeen = "";
        boolean supportsImages = false;

        ScriptedClient(McpRouter mcpRouter, ChatHistoryService chatHistoryService,
                       FaqEmbeddingService faqEmbeddingService,
                       LlmTokenUsageRepository tokenUsageRepository) {
            super(new ObjectMapper(), mcpRouter, chatHistoryService,
                    faqEmbeddingService, tokenUsageRepository);
        }

        void script(java.util.function.Function<String, LlmResponse> factory, String text) {
            script.add(factory.apply(text));
        }

        void scriptText(String text) {
            script.add(new LlmResponse(text));
        }

        void scriptToolCall(String name, Map<String, Object> arguments) {
            script.add(new LlmResponse("", List.of(
                    new LlmResponse.ToolCall(name, "call_" + name, arguments))));
        }

        void scriptApiFailure(RuntimeException failure) {
            script.add(failure);
        }

        int apiCalls() {
            return conversations.size();
        }

        List<Map<String, Object>> conversationAt(int index) {
            return conversations.get(index);
        }

        String lastFaqContextSeen() {
            return lastFaqContextSeen;
        }

        @Override
        public boolean supportsImages() {
            return supportsImages;
        }

        @Override
        protected List<Map<String, Object>> buildInitialConversation(
                String userMessage, long telegramUserId, String faqContext,
                String base64Image, String mimeType) {
            List<Map<String, Object>> conversation = new ArrayList<>();
            conversation.add(Map.of("kind", "user", "content", String.valueOf(userMessage)));
            return conversation;
        }

        @Override
        protected String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId) {
            conversations.add(List.copyOf(conversation));
            lastFaqContextSeen = faqContext != null ? faqContext : "";
            if (script.isEmpty()) {
                throw new IllegalStateException("scripted client ran out of responses");
            }
            Object next = script.peek();
            if (next instanceof RuntimeException failure) {
                script.poll();
                throw failure;
            }
            // The raw body is irrelevant here: parseResponse below pops the script.
            return "{}";
        }

        @Override
        protected LlmResponse parseResponse(String rawResponse) {
            return (LlmResponse) script.poll();
        }

        @Override
        protected void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response) {
            for (LlmResponse.ToolCall tc : response.toolCalls()) {
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("kind", "tool-call");
                entry.put("name", tc.name());
                conversation.add(entry);
            }
        }

        @Override
        protected void addToolResultToConversation(List<Map<String, Object>> conversation,
                                                   LlmResponse.ToolCall toolCall, String toolResult) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("kind", "tool-result");
            entry.put("name", toolCall.name());
            entry.put("content", toolResult);
            conversation.add(entry);
        }

        @Override
        protected void saveUsage(String rawResponse, long telegramUserId) {
            // token accounting is covered by the per-provider tests
        }

        @Override
        protected String getProviderName() {
            return "Scripted";
        }
    }
}
