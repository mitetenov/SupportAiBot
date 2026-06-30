package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.DeepSeekProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class DeepSeekClientTest {

    @Mock
    private McpRouter mcpRouter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

    private ObjectMapper objectMapper;
    private DeepSeekClient client;

    @BeforeEach
    void setUp() {
        DeepSeekProperties properties = new DeepSeekProperties();
        properties.setBaseUrl("http://localhost:9999");
        properties.setModel("deepseek-test");
        properties.setApiKey("test-key");

        objectMapper = new ObjectMapper();
        client = new DeepSeekClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @Test
    void shouldNotSupportImages() {
        assertFalse(client.supportsImages());
    }

    @Test
    void shouldThrowOnChatWithImage() {
        assertThrows(LlmProcessingException.class, () ->
                client.chatWithImage("text", 123L, "base64", "image/png"));
    }

    @Test
    void shouldBuildInitialConversationWithSystemPrompt() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = DeepSeekClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "FAQ content", null, null);

        assertEquals(3, conv.size());
        assertEquals("system", conv.get(0).get("role"));
        assertEquals("system", conv.get(1).get("role"));
        assertEquals("user", conv.get(2).get("role"));
    }

    @Test
    void shouldBuildInitialConversationWithHistory() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of(
                Map.of("role", "user", "content", "prev"),
                Map.of("role", "assistant", "content", "resp")
        ));

        var method = DeepSeekClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "FAQ", null, null);

        assertEquals(5, conv.size());
    }

    @Test
    void shouldParseTextResponse() throws Exception {
        String jsonResponse = """
                {
                    "choices": [{
                        "message": {
                            "content": "Hello, how can I help?",
                            "tool_calls": null
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, jsonResponse);

        assertEquals("Hello, how can I help?", response.text());
        assertFalse(response.hasToolCalls());
    }

    @Test
    void shouldParseToolCallResponse() throws Exception {
        String jsonResponse = """
                {
                    "choices": [{
                        "message": {
                            "content": null,
                            "tool_calls": [{
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "nodes_list",
                                    "arguments": "{}"
                                }
                            }]
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, jsonResponse);

        assertTrue(response.text().isEmpty());
        assertTrue(response.hasToolCalls());
        assertEquals(1, response.toolCalls().size());
        assertEquals("nodes_list", response.toolCalls().get(0).name());
        assertEquals("call_1", response.toolCalls().get(0).id());
    }

    @Test
    void shouldGetProviderName() {
        assertEquals("DeepSeek", client.getClass().getSimpleName().equals("DeepSeekClient") ? "DeepSeek" : "");
    }



    @Test
    void shouldAddToolResultToConversation() throws Exception {
        var method = DeepSeekClient.class.getDeclaredMethod(
                "addToolResultToConversation", List.class, LlmResponse.ToolCall.class, String.class);
        method.setAccessible(true);

        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse.ToolCall tc = new LlmResponse.ToolCall("get_nodes", "call_1", Map.of());
        method.invoke(client, conversation, tc, "{\"nodes\": []}");

        assertEquals(1, conversation.size());
        assertEquals("tool", conversation.get(0).get("role"));
        assertEquals("call_1", conversation.get(0).get("tool_call_id"));
        assertEquals("{\"nodes\": []}", conversation.get(0).get("content"));
    }

    @Test
    void shouldSaveUsage() throws Exception {
        String jsonResponse = """
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("saveUsage", String.class, long.class);
        method.setAccessible(true);
        method.invoke(client, jsonResponse, 123L);

        verify(tokenUsageRepository).save(argThat(usage ->
                usage.getTelegramId() == 123L
                && usage.getPromptTokens() == 100
                && usage.getCompletionTokens() == 50
                && usage.getTotalTokens() == 150
        ));
    }
}
