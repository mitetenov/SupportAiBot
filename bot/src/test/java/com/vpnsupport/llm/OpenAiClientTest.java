package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.OpenAiProperties;
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
class OpenAiClientTest {

    @Mock
    private McpRouter mcpRouter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

    private ObjectMapper objectMapper;
    private OpenAiClient client;

    @BeforeEach
    void setUp() {
        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl("http://localhost:9999");
        properties.setModel("openai-test");
        properties.setApiKey("test-key");
        properties.setTemperature(0.3);

        objectMapper = new ObjectMapper();
        client = new OpenAiClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @Test
    void shouldSupportImages() {
        assertTrue(client.supportsImages());
    }

    @Test
    void shouldBuildInitialConversationWithSystemPrompt() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
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

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "FAQ", null, null);

        assertEquals(5, conv.size());
    }

    @Test
    void shouldBuildConversationWithImage() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Describe this", 123L, null, "base64data", "image/png");

        assertEquals(3, conv.size());
        Map<String, Object> userMsg = conv.get(2);
        assertEquals("user", userMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Object> parts = (List<Object>) userMsg.get("content");
        assertNotNull(parts);
        assertEquals(2, parts.size());
        @SuppressWarnings("unchecked")
        Map<String, Object> textPart = (Map<String, Object>) parts.get(0);
        assertEquals("input_text", textPart.get("type"));
        assertEquals("Describe this", textPart.get("text"));
        @SuppressWarnings("unchecked")
        Map<String, Object> imagePart = (Map<String, Object>) parts.get(1);
        assertEquals("input_image", imagePart.get("type"));
        assertEquals("data:image/png;base64,base64data", imagePart.get("image_url"));
    }

    @Test
    void shouldBuildConversationWithImageAndNoText() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "", 123L, null, "base64data", "image/jpeg");

        assertEquals(3, conv.size());
        Map<String, Object> userMsg = conv.get(2);
        assertEquals("user", userMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Object> parts = (List<Object>) userMsg.get("content");
        assertEquals(1, parts.size(), "Should only have image part when text is blank");
        @SuppressWarnings("unchecked")
        Map<String, Object> imagePart = (Map<String, Object>) parts.get(0);
        assertEquals("input_image", imagePart.get("type"));
        assertEquals("data:image/jpeg;base64,base64data", imagePart.get("image_url"));
    }

    @Test
    void shouldParseTextResponse() throws Exception {
        String jsonResponse = """
                {
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": "Hello, how can I help?"
                        }]
                    }],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150
                    }
                }
                """;

        var method = OpenAiClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, jsonResponse);

        assertEquals("Hello, how can I help?", response.text());
        assertFalse(response.hasToolCalls());
    }

    @Test
    void shouldParseToolCallResponse() throws Exception {
        String jsonResponse = """
                {
                    "output": [{
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "nodes_list",
                        "arguments": "{}"
                    }],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150
                    }
                }
                """;

        var method = OpenAiClient.class.getDeclaredMethod("parseResponse", String.class);
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
        assertEquals("OpenAI", client.getProviderName());
    }

    @Test
    void shouldIncludeTelegramIdInDynamicContext() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 777L, null, null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 777"),
                "Dynamic context must contain Telegram ID: 777, got: " + dynamicContext);
        assertFalse(dynamicContext.contains("FAQ"),
                "Dynamic context must NOT contain FAQ when null");
    }

    @Test
    void shouldIncludeFaqInDynamicContextWhenPresent() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "Some FAQ content", null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 123"));
        assertTrue(dynamicContext.contains("Some FAQ content"));
    }

    @Test
    void shouldNotIncludeFaqInDynamicContextWhenEmpty() throws Exception {
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "", null, null);

        String dynamicContext = (String) conv.get(1).get("content");
        assertTrue(dynamicContext.contains("Telegram ID: 123"));
        assertFalse(dynamicContext.contains("FAQ"));
    }

    @Test
    void shouldAddToolResultToConversation() throws Exception {
        var method = OpenAiClient.class.getDeclaredMethod(
                "addToolResultToConversation", List.class, LlmResponse.ToolCall.class, String.class);
        method.setAccessible(true);

        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse.ToolCall tc = new LlmResponse.ToolCall("get_nodes", "call_1", Map.of());
        method.invoke(client, conversation, tc, "{\"nodes\": []}");

        assertEquals(1, conversation.size());
        assertEquals("function_call_output", conversation.get(0).get("type"));
        assertEquals("call_1", conversation.get(0).get("call_id"));
        assertEquals("{\"nodes\": []}", conversation.get(0).get("output"));
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

        var method = OpenAiClient.class.getDeclaredMethod("saveUsage", String.class, long.class);
        method.setAccessible(true);
        method.invoke(client, jsonResponse, 123L);

        verify(tokenUsageRepository).save(argThat(usage ->
                usage.getTelegramId() == 123L
                && usage.getPromptTokens() == 100
                && usage.getCompletionTokens() == 50
                && usage.getTotalTokens() == 150
        ));
    }

    @Test
    void shouldRejectNullApiKey() {
        OpenAiProperties props = new OpenAiProperties();
        props.setBaseUrl("http://localhost:9999");
        props.setModel("test");
        props.setApiKey(null);

        assertThrows(IllegalArgumentException.class, () ->
                new OpenAiClient(props, objectMapper, mcpRouter,
                        chatHistoryService, faqEmbeddingService, tokenUsageRepository));
    }

    @Test
    void shouldRejectBlankApiKey() {
        OpenAiProperties props = new OpenAiProperties();
        props.setBaseUrl("http://localhost:9999");
        props.setModel("test");
        props.setApiKey("   ");

        assertThrows(IllegalArgumentException.class, () ->
                new OpenAiClient(props, objectMapper, mcpRouter,
                        chatHistoryService, faqEmbeddingService, tokenUsageRepository));
    }

    @Test
    void shouldRejectEmptyApiKey() {
        OpenAiProperties props = new OpenAiProperties();
        props.setBaseUrl("http://localhost:9999");
        props.setModel("test");
        props.setApiKey("");

        assertThrows(IllegalArgumentException.class, () ->
                new OpenAiClient(props, objectMapper, mcpRouter,
                        chatHistoryService, faqEmbeddingService, tokenUsageRepository));
    }

    @Test
    void shouldIncludeReasoningEffortWhenToolsPresent() throws Exception {
        when(mcpRouter.listTools()).thenReturn(List.of(
                new McpTool("test_tool", "test description", Map.of())
        ));

        var method = OpenAiClient.class.getDeclaredMethod("buildRequestBody", List.class);
        method.setAccessible(true);

        List<Map<String, Object>> messages = List.of(Map.of("role", "user", "content", "hi"));
        ObjectNode body = (ObjectNode) method.invoke(client, messages);

        assertTrue(body.has("reasoning_effort"), "Should include reasoning_effort when tools are present");
        assertEquals("none", body.get("reasoning_effort").asText());
        assertTrue(body.has("tools"));
        assertTrue(body.has("tool_choice"));
        assertEquals("auto", body.get("tool_choice").asText());
    }

    @Test
    void shouldNotIncludeReasoningEffortWhenNoTools() throws Exception {
        when(mcpRouter.listTools()).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod("buildRequestBody", List.class);
        method.setAccessible(true);

        List<Map<String, Object>> messages = List.of(Map.of("role", "user", "content", "hi"));
        ObjectNode body = (ObjectNode) method.invoke(client, messages);

        assertFalse(body.has("reasoning_effort"), "Should NOT include reasoning_effort without tools");
        assertFalse(body.has("tools"));
        assertFalse(body.has("tool_choice"));
    }

    @Test
    void shouldIncludeTemperatureInRequestBody() throws Exception {
        when(mcpRouter.listTools()).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod("buildRequestBody", List.class);
        method.setAccessible(true);

        ObjectNode body = (ObjectNode) method.invoke(client, List.of(Map.of("role", "user", "content", "hi")));

        assertTrue(body.has("temperature"));
        assertEquals(0.3, body.get("temperature").asDouble(), 0.001);
    }

    @Test
    void shouldIncludeModelInRequestBody() throws Exception {
        when(mcpRouter.listTools()).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod("buildRequestBody", List.class);
        method.setAccessible(true);

        ObjectNode body = (ObjectNode) method.invoke(client, List.of(Map.of("role", "user", "content", "hi")));

        assertEquals("openai-test", body.get("model").asText());
    }

    @Test
    void shouldSerializeToolCallArgumentsToJsonString() throws Exception {
        Map<String, Object> complexArgs = Map.of(
                "key1", "value1",
                "key2", 42,
                "nested", Map.of("inner", "data")
        );

        String expectedJson = objectMapper.writeValueAsString(complexArgs);

        var method = OpenAiClient.class.getDeclaredMethod(
                "addToolCallsToConversation", List.class, LlmResponse.class);
        method.setAccessible(true);

        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("test_func", "call_123", complexArgs)
        ));

        method.invoke(client, conversation, response);

        assertEquals(1, conversation.size());
        assertEquals("function_call", conversation.get(0).get("type"));
        assertEquals("call_123", conversation.get(0).get("call_id"));
        assertEquals("test_func", conversation.get(0).get("name"));
        assertEquals(expectedJson, conversation.get(0).get("arguments"));
    }

    @Test
    void shouldHandleEmptyArgumentsInToolCall() throws Exception {
        var method = OpenAiClient.class.getDeclaredMethod(
                "addToolCallsToConversation", List.class, LlmResponse.class);
        method.setAccessible(true);

        List<Map<String, Object>> conversation = new java.util.ArrayList<>();
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("empty_func", "call_1", Map.of())
        ));

        method.invoke(client, conversation, response);

        assertEquals(1, conversation.size());
        assertEquals("function_call", conversation.get(0).get("type"));
        assertEquals("call_1", conversation.get(0).get("call_id"));
        assertEquals("empty_func", conversation.get(0).get("name"));
        assertEquals("{}", conversation.get(0).get("arguments"));
    }

    @Test
    void shouldIncludeMessagesInRequestBody() throws Exception {
        when(mcpRouter.listTools()).thenReturn(List.of());

        var method = OpenAiClient.class.getDeclaredMethod("buildRequestBody", List.class);
        method.setAccessible(true);

        List<Map<String, Object>> messages = List.of(
                Map.of("role", "system", "content", "You are helpful"),
                Map.of("role", "user", "content", "hello")
        );
        ObjectNode body = (ObjectNode) method.invoke(client, messages);

        assertTrue(body.has("messages"));
        assertEquals(2, body.get("messages").size());
    }
}
