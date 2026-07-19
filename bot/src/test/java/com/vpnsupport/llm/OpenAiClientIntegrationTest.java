package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.OpenAiProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class OpenAiClientIntegrationTest {

    @Mock
    private McpRouter mcpRouter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

    private HttpServer mockServer;
    private OpenAiClient client;
    private ObjectMapper objectMapper;
    private String baseUrl;
    private volatile String capturedRequestBody;
    private volatile String responseToReturn;
    private volatile int responseStatusCode;

    @BeforeEach
    void setUp() throws Exception {
        objectMapper = new ObjectMapper();
        capturedRequestBody = null;
        responseStatusCode = 200;

        // Start a mock HTTP server on a random port
        mockServer = HttpServer.create(new InetSocketAddress(0), 0);
        mockServer.createContext("/chat/completions", this::handleChatCompletions);
        mockServer.setExecutor(null);
        mockServer.start();

        int port = mockServer.getAddress().getPort();
        baseUrl = "http://localhost:" + port;

        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl(baseUrl);
        properties.setModel("gpt-5.4-mini");
        properties.setApiKey("sk-test-integration");
        properties.setTemperature(0.3);

        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());

        client = new OpenAiClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @AfterEach
    void tearDown() {
        if (mockServer != null) {
            mockServer.stop(0);
        }
    }

    private void handleChatCompletions(HttpExchange exchange) throws IOException {
        // Read the request body
        capturedRequestBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);

        // Verify authorization header
        String auth = exchange.getRequestHeaders().getFirst("Authorization");
        assertEquals("Bearer sk-test-integration", auth, "Authorization header must contain the API key");
        assertEquals("POST", exchange.getRequestMethod(), "Must be a POST request");

        // Build response
        byte[] responseBytes;
        if (responseToReturn != null) {
            responseBytes = responseToReturn.getBytes(StandardCharsets.UTF_8);
        } else {
            responseBytes = "{}".getBytes(StandardCharsets.UTF_8);
        }

        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(responseStatusCode, responseBytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(responseBytes);
        }
    }

    @Test
    void shouldSendCorrectRequestFormat() throws Exception {
        responseToReturn = """
                {
                    "choices": [{
                        "message": {
                            "content": "Test response",
                            "tool_calls": null
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15
                    }
                }
                """;

        when(mcpRouter.listTools()).thenReturn(List.of());

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "Hello", 123L, null, null, null);

        String result = client.callApi(conversation, null, 123L);

        assertNotNull(capturedRequestBody, "Request body must have been captured");
        var requestJson = objectMapper.readTree(capturedRequestBody);

        assertEquals("gpt-5.4-mini", requestJson.get("model").asText());
        assertTrue(requestJson.has("messages"), "Request must have messages array");
        assertEquals(3, requestJson.get("messages").size());
        assertEquals(0.3, requestJson.get("temperature").asDouble(), 0.001);
        assertFalse(requestJson.has("tools"), "Tools key should be absent when mcpRouter has no tools");

        // Verify response was parsed
        assertTrue(result.contains("Test response"));
    }

    @Test
    void shouldIncludeToolsInRequestWhenAvailable() throws Exception {
        responseToReturn = """
                {
                    "choices": [{
                        "message": {
                            "content": null,
                            "tool_calls": [{
                                "id": "call_int_1",
                                "type": "function",
                                "function": {
                                    "name": "nodes_list",
                                    "arguments": "{}"
                                }
                            }]
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "total_tokens": 30
                    }
                }
                """;

        when(mcpRouter.listTools()).thenReturn(List.of(
                new McpTool("nodes_list", "List all nodes", Map.of("type", "object", "properties", Map.of()))
        ));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "List nodes", 123L, null, null, null);

        String result = client.callApi(conversation, null, 123L);

        var requestJson = objectMapper.readTree(capturedRequestBody);

        assertEquals("gpt-5.4-mini", requestJson.get("model").asText());
        assertTrue(requestJson.has("tools"));
        assertEquals(1, requestJson.get("tools").size());
        assertEquals("nodes_list", requestJson.get("tools").get(0).get("function").get("name").asText());
        assertEquals("auto", requestJson.get("tool_choice").asText());

        // Verify tool call was parsed
        assertTrue(result.contains("call_int_1"));
        assertTrue(result.contains("nodes_list"));
    }

    @Test
    void shouldHandleHttpErrorFromApi() throws Exception {
        responseStatusCode = 401;
        responseToReturn = "{\"error\": {\"message\": \"Invalid API key\", \"type\": \"invalid_request_error\"}}";

        when(mcpRouter.listTools()).thenReturn(List.of());

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "Hello", 123L, null, null, null);

        assertThrows(RuntimeException.class, () ->
                client.callApi(conversation, null, 123L));
    }

    @Test
    void shouldSendValidRequestBody() throws Exception {
        responseToReturn = """
                {
                    "choices": [{
                        "message": {
                            "content": "OK",
                            "tool_calls": null
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15
                    }
                }
                """;

        when(mcpRouter.listTools()).thenReturn(List.of());

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "Hello", 123L, null, null, null);

        client.callApi(conversation, null, 123L);

        // Validate the request body is valid JSON with correct structure
        var requestJson = objectMapper.readTree(capturedRequestBody);

        // Check messages have correct roles
        var messages = requestJson.get("messages");
        assertEquals("system", messages.get(0).get("role").asText());
        assertEquals("system", messages.get(1).get("role").asText());
        assertEquals("user", messages.get(2).get("role").asText());
        assertEquals("Hello", messages.get(2).get("content").asText());

        // Check temperature
        assertEquals(0.3, requestJson.get("temperature").asDouble(), 0.001);
    }
}
