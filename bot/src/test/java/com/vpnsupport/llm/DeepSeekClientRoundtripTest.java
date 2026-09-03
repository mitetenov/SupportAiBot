package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.DeepSeekProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(MockitoExtension.class)
class DeepSeekClientRoundtripTest {

    @Mock private McpRouter mcpRouter;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private FaqEmbeddingService faqEmbeddingService;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;

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
    void shouldParseTextOnlyResponse() throws Exception {
        String rawResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "Your VPN is working fine."
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, rawResponse);

        assertEquals("Your VPN is working fine.", response.text());
        assertFalse(response.hasToolCalls());
        assertTrue(response.rawParts().isEmpty());
    }

    @Test
    void shouldParseToolCallResponse() throws Exception {
        String rawResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": null,
                            "tool_calls": [
                                {
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "nodes_list",
                                        "arguments": "{}"
                                    }
                                },
                                {
                                    "id": "call_def456",
                                    "type": "function",
                                    "function": {
                                        "name": "users_get_by_telegram_id",
                                        "arguments": "{\\"telegramId\\": 12345}"
                                    }
                                }
                            ]
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 15,
                        "total_tokens": 35
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, rawResponse);

        assertTrue(response.text().isEmpty() || response.text() == null);
        assertEquals(2, response.toolCalls().size());

        LlmResponse.ToolCall tc1 = response.toolCalls().get(0);
        assertEquals("nodes_list", tc1.name());
        assertEquals("call_abc123", tc1.id());
        assertTrue(tc1.arguments().isEmpty());

        LlmResponse.ToolCall tc2 = response.toolCalls().get(1);
        assertEquals("users_get_by_telegram_id", tc2.name());
        assertEquals("call_def456", tc2.id());
        assertEquals(12345, tc2.arguments().get("telegramId"));
    }

    @Test
    void shouldBuildCorrectToolCallsInConversation() throws Exception {
        LlmResponse.ToolCall tc1 = new LlmResponse.ToolCall("nodes_list", "call_1", Map.of());
        LlmResponse.ToolCall tc2 = new LlmResponse.ToolCall("users_get", "call_2", Map.of("telegramId", 555));
        LlmResponse response = new LlmResponse("Checking...", List.of(tc1, tc2));

        List<Map<String, Object>> conversation = new ArrayList<>();
        var method = DeepSeekClient.class.getDeclaredMethod(
                "addToolCallsToConversation", List.class, LlmResponse.class);
        method.setAccessible(true);
        method.invoke(client, conversation, response);

        assertEquals(1, conversation.size());
        Map<String, Object> assistantMsg = conversation.get(0);
        assertEquals("assistant", assistantMsg.get("role"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> toolCalls = (List<Map<String, Object>>) assistantMsg.get("tool_calls");
        assertEquals(2, toolCalls.size());

        Map<String, Object> call1 = toolCalls.get(0);
        assertEquals("call_1", call1.get("id"));
        @SuppressWarnings("unchecked")
        Map<String, Object> fn1 = (Map<String, Object>) call1.get("function");
        assertEquals("nodes_list", fn1.get("name"));

        Map<String, Object> call2 = toolCalls.get(1);
        assertEquals("call_2", call2.get("id"));
        @SuppressWarnings("unchecked")
        Map<String, Object> fn2 = (Map<String, Object>) call2.get("function");
        assertEquals("users_get", fn2.get("name"));
        assertEquals("{\"telegramId\":555}", fn2.get("arguments"));
    }

    @Test
    void shouldBuildCorrectToolResultInConversation() throws Exception {
        LlmResponse.ToolCall tc = new LlmResponse.ToolCall("nodes_list", "call_xyz", Map.of());
        List<Map<String, Object>> conversation = new ArrayList<>();

        var method = DeepSeekClient.class.getDeclaredMethod(
                "addToolResultToConversation", List.class, LlmResponse.ToolCall.class, String.class);
        method.setAccessible(true);
        method.invoke(client, conversation, tc, "{\"nodes\": [{\"name\": \"Germany\"}]}");

        assertEquals(1, conversation.size());
        Map<String, Object> toolMsg = conversation.get(0);
        assertEquals("tool", toolMsg.get("role"));
        assertEquals("call_xyz", toolMsg.get("tool_call_id"));
        assertEquals("{\"nodes\": [{\"name\": \"Germany\"}]}", toolMsg.get("content"));
    }

    @Test
    void shouldDoFullToolCallRoundtrip() throws Exception {
        String apiResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": null,
                            "tool_calls": [{
                                "id": "call_full_1",
                                "type": "function",
                                "function": {
                                    "name": "hwid_devices_list",
                                    "arguments": "{\\"uuid\\": \\"abc-123\\"}"
                                }
                            }]
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 15,
                        "total_tokens": 35
                    }
                }
                """;

        // Step 1: Parse
        var parseMethod = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        parseMethod.setAccessible(true);
        LlmResponse response = (LlmResponse) parseMethod.invoke(client, apiResponse);

        // Step 2: Add tool calls to conversation
        List<Map<String, Object>> conversation = new ArrayList<>();
        var addCallsMethod = DeepSeekClient.class.getDeclaredMethod(
                "addToolCallsToConversation", List.class, LlmResponse.class);
        addCallsMethod.setAccessible(true);
        addCallsMethod.invoke(client, conversation, response);

        // Step 3: Add tool result
        LlmResponse.ToolCall tc = response.toolCalls().get(0);
        var addResultMethod = DeepSeekClient.class.getDeclaredMethod(
                "addToolResultToConversation", List.class, LlmResponse.ToolCall.class, String.class);
        addResultMethod.setAccessible(true);
        addResultMethod.invoke(client, conversation, tc, "{\"devices\": []}");

        assertEquals(2, conversation.size());

        // Assistant message
        Map<String, Object> asstMsg = conversation.get(0);
        assertEquals("assistant", asstMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> tcs = (List<Map<String, Object>>) asstMsg.get("tool_calls");
        assertEquals("call_full_1", tcs.get(0).get("id"));

        // Tool message
        Map<String, Object> toolMsg = conversation.get(1);
        assertEquals("tool", toolMsg.get("role"));
        assertEquals("call_full_1", toolMsg.get("tool_call_id"));
    }

    @Test
    void shouldParseMixedTextAndToolCallResponse() throws Exception {
        String rawResponse = """
                {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "Let me check that for you.",
                            "tool_calls": [{
                                "id": "call_mixed",
                                "type": "function",
                                "function": {
                                    "name": "nodes_list",
                                    "arguments": "{}"
                                }
                            }]
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 15,
                        "completion_tokens": 20,
                        "total_tokens": 35
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, rawResponse);

        assertEquals("Let me check that for you.", response.text());
        assertEquals(1, response.toolCalls().size());
        assertEquals("nodes_list", response.toolCalls().get(0).name());
    }

    @Test
    void shouldHandleToolCallWithComplexArguments() throws Exception {
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
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30
                    }
                }
                """;

        var method = DeepSeekClient.class.getDeclaredMethod("parseResponse", String.class);
        method.setAccessible(true);
        LlmResponse response = (LlmResponse) method.invoke(client, rawResponse);

        LlmResponse.ToolCall tc = response.toolCalls().get(0);
        assertEquals("call_complex", tc.id());
        assertEquals(3, tc.arguments().size());
        assertEquals("DE", tc.arguments().get("countryCode"));
        assertEquals("CONNECTED", tc.arguments().get("status"));
        assertEquals(10, tc.arguments().get("limit"));
    }

    @Test
    void shouldReturnProviderName() throws Exception {
        var method = DeepSeekClient.class.getDeclaredMethod("getProviderName");
        method.setAccessible(true);
        assertEquals("DeepSeek", method.invoke(client));
    }
}
