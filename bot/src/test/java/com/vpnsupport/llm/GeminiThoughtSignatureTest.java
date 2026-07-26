package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.GeminiProperties;
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

/**
 * Gemini attaches an opaque {@code thought_signature} to a function call and
 * expects it echoed back on the matching function response. Dropping it breaks
 * multi-step tool use, and nothing else in the codebase touches this field —
 * hence a file of its own.
 */
@ExtendWith(MockitoExtension.class)
class GeminiThoughtSignatureTest {

    @Mock private McpRouter mcpRouter;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private FaqEmbeddingService faqEmbeddingService;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;

    private ObjectMapper objectMapper;
    private GeminiClient client;

    @BeforeEach
    void setUp() {
        GeminiProperties properties = new GeminiProperties();
        properties.setBaseUrl("http://localhost:9999");
        properties.setModel("gemini-test");
        properties.setApiKey("test-key");

        objectMapper = new ObjectMapper();
        client = new GeminiClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @Test
    void shouldParseResponseWithThoughtSignature() throws Exception {
        String rawResponse = """
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Let me check your devices."},
                                {
                                    "functionCall": {
                                        "name": "hwid_devices_list",
                                        "args": {"uuid": "abc-123"},
                                        "thought_signature": "sig_abc123xyz"
                                    }
                                },
                                {
                                    "functionCall": {
                                        "name": "nodes_list",
                                        "args": {},
                                        "thought_signature": "sig_def456uvw"
                                    }
                                }
                            ]
                        }
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "candidatesTokenCount": 50,
                        "totalTokenCount": 150
                    }
                }
                """;

        LlmResponse response = client.parseResponse(rawResponse);

        assertEquals("Let me check your devices.", response.text());
        assertEquals(2, response.toolCalls().size());

        // Verify tool calls have thought_signature
        LlmResponse.ToolCall tc1 = response.toolCalls().get(0);
        assertEquals("hwid_devices_list", tc1.name());
        assertEquals("sig_abc123xyz", tc1.thoughtSignature());
        assertEquals(1, tc1.arguments().size());
        assertEquals("abc-123", tc1.arguments().get("uuid"));

        LlmResponse.ToolCall tc2 = response.toolCalls().get(1);
        assertEquals("nodes_list", tc2.name());
        assertEquals("sig_def456uvw", tc2.thoughtSignature());

        // Verify rawParts are captured as-is (key for preserving thought_signature)
        assertEquals(3, response.rawParts().size());

        Map<String, Object> textPart = response.rawParts().get(0);
        assertEquals("Let me check your devices.", textPart.get("text"));

        @SuppressWarnings("unchecked")
        Map<String, Object> fc1 = (Map<String, Object>) response.rawParts().get(1).get("functionCall");
        assertEquals("hwid_devices_list", fc1.get("name"));
        assertEquals("sig_abc123xyz", fc1.get("thought_signature"));

        @SuppressWarnings("unchecked")
        Map<String, Object> fc2 = (Map<String, Object>) response.rawParts().get(2).get("functionCall");
        assertEquals("nodes_list", fc2.get("name"));
        assertEquals("sig_def456uvw", fc2.get("thought_signature"));
    }

    @Test
    void shouldPreserveThoughtSignatureInModelParts() throws Exception {
        String rawResponse = """
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "hwid_devices_list",
                                        "args": {"uuid": "abc-123"},
                                        "thought_signature": "sig_preserve_me"
                                    }
                                }
                            ]
                        }
                    }],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
                }
                """;

        LlmResponse response = client.parseResponse(rawResponse);

        // Now add to conversation — should use rawParts and preserve thought_signature
        List<Map<String, Object>> conversation = new ArrayList<>();
        client.addToolCallsToConversation(conversation, response);

        assertEquals(1, conversation.size());
        Map<String, Object> modelMsg = conversation.get(0);
        assertEquals("model", modelMsg.get("role"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> modelParts = (List<Map<String, Object>>) modelMsg.get("parts");
        assertEquals(1, modelParts.size());

        @SuppressWarnings("unchecked")
        Map<String, Object> functionCall = (Map<String, Object>) modelParts.get(0).get("functionCall");
        assertNotNull(functionCall, "functionCall should be present");
        assertEquals("hwid_devices_list", functionCall.get("name"));
        assertEquals("sig_preserve_me", functionCall.get("thought_signature"),
                "thought_signature MUST be preserved in model parts");
    }

    @Test
    void shouldIncludeThoughtSignatureInFunctionResponse() throws Exception {
        LlmResponse.ToolCall toolCall = new LlmResponse.ToolCall(
                "hwid_devices_list", "", Map.of("uuid", "abc-123"), "sig_response_test");

        List<Map<String, Object>> conversation = new ArrayList<>();
        client.addToolResultToConversation(conversation, toolCall, "{\"devices\": []}");

        assertEquals(1, conversation.size());
        Map<String, Object> funcMsg = conversation.get(0);
        assertEquals("function", funcMsg.get("role"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> parts = (List<Map<String, Object>>) funcMsg.get("parts");
        assertEquals(1, parts.size());

        @SuppressWarnings("unchecked")
        Map<String, Object> funcResponse = (Map<String, Object>) parts.get(0).get("functionResponse");
        assertNotNull(funcResponse);
        assertEquals("hwid_devices_list", funcResponse.get("name"));
        assertEquals("sig_response_test", funcResponse.get("thought_signature"),
                "thought_signature MUST be present in functionResponse");

        @SuppressWarnings("unchecked")
        Map<String, Object> responseContent = (Map<String, Object>) funcResponse.get("response");
        assertNotNull(responseContent);
    }

    @Test
    void shouldNotIncludeThoughtSignatureWhenNull() throws Exception {
        LlmResponse.ToolCall toolCall = new LlmResponse.ToolCall(
                "nodes_list", "", Map.of(), null);

        List<Map<String, Object>> conversation = new ArrayList<>();
        client.addToolResultToConversation(conversation, toolCall, "[]");

        Map<String, Object> funcMsg = conversation.get(0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> parts = (List<Map<String, Object>>) funcMsg.get("parts");
        @SuppressWarnings("unchecked")
        Map<String, Object> funcResponse = (Map<String, Object>) parts.get(0).get("functionResponse");
        assertNull(funcResponse.get("thought_signature"),
                "thought_signature should be absent when null, not 'null' string");
    }

    @Test
    void shouldDoFullFunctionCallRoundtripWithThoughtSignature() throws Exception {
        // Simulate a complete function-call cycle: model calls tool → we execute → we respond
        String apiResponse = """
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Checking your account..."},
                                {
                                    "functionCall": {
                                        "name": "users_get_by_telegram_id",
                                        "args": {"telegramId": 12345},
                                        "thought_signature": "sig_full_roundtrip"
                                    }
                                }
                            ]
                        }
                    }],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 30, "totalTokenCount": 80}
                }
                """;

        // Step 1: Parse the response
        LlmResponse response = client.parseResponse(apiResponse);

        // Step 2: Add model's function call to conversation
        List<Map<String, Object>> conversation = new ArrayList<>();
        client.addToolCallsToConversation(conversation, response);

        // Step 3: Add function response
        LlmResponse.ToolCall tc = response.toolCalls().get(0);
        client.addToolResultToConversation(conversation, tc, "{\"uuid\": \"user-uuid-123\"}");

        // Assert: conversation now contains model message + function response
        assertEquals(2, conversation.size());

        // Model message should have thought_signature in functionCall
        Map<String, Object> modelMsg = conversation.get(0);
        assertEquals("model", modelMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> modelParts = (List<Map<String, Object>>) modelMsg.get("parts");
        @SuppressWarnings("unchecked")
        Map<String, Object> fc = (Map<String, Object>) modelParts.get(1).get("functionCall");
        assertEquals("sig_full_roundtrip", fc.get("thought_signature"),
                "Model parts must preserve thought_signature from API response");

        // Function response should echo the same thought_signature
        Map<String, Object> funcMsg = conversation.get(1);
        assertEquals("function", funcMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> funcParts = (List<Map<String, Object>>) funcMsg.get("parts");
        @SuppressWarnings("unchecked")
        Map<String, Object> funcResponse = (Map<String, Object>) funcParts.get(0).get("functionResponse");
        assertEquals("sig_full_roundtrip", funcResponse.get("thought_signature"),
                "Function response must echo thought_signature from function call");
    }

    @Test
    void shouldFallbackWhenRawPartsAreEmpty() throws Exception {
        // If for some reason rawParts are empty, should build parts from parsed ToolCalls
        LlmResponse response = new LlmResponse("Just text", List.of(), List.of());
        List<Map<String, Object>> conversation = new ArrayList<>();

        client.addToolCallsToConversation(conversation, response);

        assertEquals(1, conversation.size());
        Map<String, Object> modelMsg = conversation.get(0);
        assertEquals("model", modelMsg.get("role"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> modelParts = (List<Map<String, Object>>) modelMsg.get("parts");
        assertEquals(1, modelParts.size());
        assertEquals("Just text", modelParts.get(0).get("text"));
    }

    @Test
    void shouldHandleToolCallWithoutThoughtSignature() throws Exception {
        String rawResponse = """
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "simple_tool",
                                        "args": {}
                                    }
                                }
                            ]
                        }
                    }],
                    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7}
                }
                """;

        LlmResponse response = client.parseResponse(rawResponse);

        assertEquals(1, response.toolCalls().size());
        assertNull(response.toolCalls().get(0).thoughtSignature(),
                "thoughtSignature should be null when not present in API response");

        // Verify rawPart still has the functionCall but no thought_signature
        @SuppressWarnings("unchecked")
        Map<String, Object> fc = (Map<String, Object>) response.rawParts().get(0).get("functionCall");
        assertNull(fc.get("thought_signature"));
    }

    @Test
    void shouldHandleMultipleSimultaneousFunctionCallsWithMixedSignatures() throws Exception {
        String rawResponse = """
                {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Checking multiple things..."},
                                {
                                    "functionCall": {
                                        "name": "nodes_list",
                                        "args": {},
                                        "thought_signature": "sig_nodes"
                                    }
                                },
                                {
                                    "functionCall": {
                                        "name": "users_get_by_telegram_id",
                                        "args": {"telegramId": 555},
                                        "thought_signature": "sig_users"
                                    }
                                }
                            ]
                        }
                    }],
                    "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 10, "totalTokenCount": 30}
                }
                """;

        LlmResponse response = client.parseResponse(rawResponse);

        assertEquals(2, response.toolCalls().size());
        assertEquals("sig_nodes", response.toolCalls().get(0).thoughtSignature());
        assertEquals("sig_users", response.toolCalls().get(1).thoughtSignature());

        // Verify rawParts preserve both signatures
        List<Map<String, Object>> conversation = new ArrayList<>();
        client.addToolCallsToConversation(conversation, response);

        Map<String, Object> modelMsg = conversation.get(0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> modelParts = (List<Map<String, Object>>) modelMsg.get("parts");
        assertEquals(3, modelParts.size()); // text + 2 functionCalls

        @SuppressWarnings("unchecked")
        Map<String, Object> fc1 = (Map<String, Object>) modelParts.get(1).get("functionCall");
        assertEquals("sig_nodes", fc1.get("thought_signature"));

        @SuppressWarnings("unchecked")
        Map<String, Object> fc2 = (Map<String, Object>) modelParts.get(2).get("functionCall");
        assertEquals("sig_users", fc2.get("thought_signature"));
    }
}
