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

        List<Map<String, Object>> conv = client.buildInitialConversation("Hello", 123L, "FAQ content", null, null);

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

        List<Map<String, Object>> conv = client.buildInitialConversation("Hello", 123L, "FAQ", null, null);

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

        LlmResponse response = client.parseResponse(jsonResponse);

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

        LlmResponse response = client.parseResponse(jsonResponse);

        assertTrue(response.text().isEmpty());
        assertTrue(response.hasToolCalls());
        assertEquals(1, response.toolCalls().size());
        assertEquals("nodes_list", response.toolCalls().get(0).name());
        assertEquals("call_1", response.toolCalls().get(0).id());
    }

    @Test
    void shouldGetProviderName() {
        assertEquals("DeepSeek", client.getProviderName());
    }



    @Test
    void shouldIncludeTelegramIdInDynamicContext() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        List<Map<String, Object>> conv = client.buildInitialConversation("Hello", 777L, null, null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 777"),
                "Dynamic context must contain Telegram ID: 777, got: " + dynamicContext);
        assertTrue(!dynamicContext.contains("FAQ"),
                "Dynamic context must NOT contain FAQ when null");
    }

    @Test
    void shouldIncludeFaqInDynamicContextWhenPresent() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        List<Map<String, Object>> conv = client.buildInitialConversation("Hello", 123L, "Some FAQ content", null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 123"));
        assertTrue(dynamicContext.contains("Some FAQ content"));
    }

    @Test
    void shouldNotIncludeFaqInDynamicContextWhenEmpty() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        List<Map<String, Object>> conv = client.buildInitialConversation("Hello", 123L, "", null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 123"));
        assertTrue(!dynamicContext.contains("FAQ"));
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

        client.saveUsage(jsonResponse, 123L);

        verify(tokenUsageRepository).save(argThat(usage ->
                usage.getTelegramId() == 123L
                && usage.getPromptTokens() == 100
                && usage.getCompletionTokens() == 50
                && usage.getTotalTokens() == 150
        ));
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldSerializeToolCallArgumentsAsJsonString() {
        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("nodes_get", "call_1", Map.of("uuid", "abc-123"))));

        client.addToolCallsToConversation(conversation, response);

        assertEquals(1, conversation.size());
        Map<String, Object> assistantMessage = conversation.get(0);
        assertEquals("assistant", assistantMessage.get("role"));

        List<Map<String, Object>> toolCalls = (List<Map<String, Object>>) assistantMessage.get("tool_calls");
        Map<String, Object> function = (Map<String, Object>) toolCalls.get(0).get("function");

        Object arguments = function.get("arguments");
        assertInstanceOf(String.class, arguments,
                "the OpenAI-compatible schema types function.arguments as a JSON string");
        assertEquals("{\"uuid\":\"abc-123\"}", arguments);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldSerializeEmptyToolCallArgumentsAsEmptyJsonObject() {
        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("nodes_list", "call_1", Map.of())));

        client.addToolCallsToConversation(conversation, response);

        List<Map<String, Object>> toolCalls =
                (List<Map<String, Object>>) conversation.get(0).get("tool_calls");
        Map<String, Object> function = (Map<String, Object>) toolCalls.get(0).get("function");

        assertEquals("{}", function.get("arguments"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldRoundTripToolCallArgumentsBackToAMap() throws Exception {
        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("users_get_by_telegram_id", "call_1",
                        Map.of("telegramId", 12345))));

        client.addToolCallsToConversation(conversation, response);

        List<Map<String, Object>> toolCalls =
                (List<Map<String, Object>>) conversation.get(0).get("tool_calls");
        Map<String, Object> function = (Map<String, Object>) toolCalls.get(0).get("function");

        Map<String, Object> reparsed = objectMapper.readValue((String) function.get("arguments"), Map.class);
        assertEquals(12345, reparsed.get("telegramId"));
    }

    @Test
    void shouldParseAResponseCarryingBothTextAndAToolCall() {
        String rawResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "Let me check that for you.",
                            "tool_calls": [{
                                "id": "call_mixed",
                                "type": "function",
                                "function": {"name": "nodes_list", "arguments": "{}"}
                            }]
                        }
                    }]
                }
                """;

        LlmResponse response = client.parseResponse(rawResponse);

        assertEquals("Let me check that for you.", response.text());
        assertEquals(1, response.toolCalls().size());
        assertEquals("nodes_list", response.toolCalls().get(0).name());
    }

    @Test
    void shouldParseToolCallArgumentsOfEveryJsonType() {
        String rawResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": null,
                            "tool_calls": [{
                                "id": "call_complex",
                                "type": "function",
                                "function": {
                                    "name": "filter_nodes",
                                    "arguments": "{\\"countryCode\\": \\"DE\\", \\"status\\": \\"CONNECTED\\", \\"limit\\": 10}"
                                }
                            }]
                        }
                    }]
                }
                """;

        LlmResponse.ToolCall tc = client.parseResponse(rawResponse).toolCalls().get(0);

        assertEquals("call_complex", tc.id());
        assertEquals("DE", tc.arguments().get("countryCode"));
        assertEquals("CONNECTED", tc.arguments().get("status"));
        assertEquals(10, tc.arguments().get("limit"));
    }
}
