package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
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

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.lenient;
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
        mockServer.createContext("/responses", this::handleResponses);
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
        lenient().when(mcpRouter.listTools()).thenReturn(List.of());

        client = newClient();
    }

    /**
     * Builds a client against the current {@code mcpRouter} stubbing. Tool
     * definitions are resolved in the constructor, so a test that needs a
     * non-empty tool list must re-stub and rebuild.
     */
    private OpenAiClient newClient() {
        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl(baseUrl);
        properties.setModel("gpt-5.4-mini");
        properties.setApiKey("sk-test-integration");
        properties.setTemperature(0.3);
        return new OpenAiClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @AfterEach
    void tearDown() {
        if (mockServer != null) {
            mockServer.stop(0);
        }
    }

    private void handleResponses(HttpExchange exchange) throws IOException {
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
                    "output": [{
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Test response"}]
                    }],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15
                    }
                }
                """;

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "Hello", 123L, null, null, null);

        String result = client.callApi(conversation, null, 123L);

        assertNotNull(capturedRequestBody, "Request body must have been captured");
        var requestJson = objectMapper.readTree(capturedRequestBody);

        assertEquals("gpt-5.4-mini", requestJson.get("model").asText());
        assertTrue(requestJson.has("input"), "Request must have input array");
        assertFalse(requestJson.has("tools"), "Tools key should be absent when mcpRouter has no tools");

        assertTrue(result.contains("Test response"));
    }

    @Test
    void shouldIncludeToolsInRequestWhenAvailable() throws Exception {
        responseToReturn = """
                {
                    "output": [{
                        "type": "function_call",
                        "call_id": "call_int_1",
                        "name": "nodes_list",
                        "arguments": "{}"
                    }],
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 30
                    }
                }
                """;

        when(mcpRouter.listTools()).thenReturn(List.of(
                new McpTool("nodes_list", "List all nodes", Map.of("type", "object", "properties", Map.of()))
        ));
        client = newClient();

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "List nodes", 123L, null, null, null);

        String result = client.callApi(conversation, null, 123L);

        var requestJson = objectMapper.readTree(capturedRequestBody);

        assertEquals("gpt-5.4-mini", requestJson.get("model").asText());
        assertTrue(requestJson.has("tools"));
        assertEquals(1, requestJson.get("tools").size());
        assertEquals("nodes_list", requestJson.get("tools").get(0).get("name").asText());
        assertEquals("auto", requestJson.get("tool_choice").asText());
        assertEquals("none", requestJson.get("reasoning").get("effort").asText());

        assertTrue(result.contains("call_int_1"));
        assertTrue(result.contains("nodes_list"));
    }

    @Test
    void shouldHandleHttpErrorFromApi() throws Exception {
        responseStatusCode = 401;
        responseToReturn = "{\"error\": {\"message\": \"Invalid API key\", \"type\": \"invalid_request_error\"}}";

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
                    "output": [{
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}]
                    }],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15
                    }
                }
                """;

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conversation = (List<Map<String, Object>>) OpenAiClient.class
                .getDeclaredMethod("buildInitialConversation", String.class, long.class, String.class, String.class, String.class)
                .invoke(client, "Hello", 123L, null, null, null);

        client.callApi(conversation, null, 123L);

        var requestJson = objectMapper.readTree(capturedRequestBody);

        var input = requestJson.get("input");
        assertEquals("system", input.get(0).get("role").asText());
        assertEquals("user", input.get(input.size() - 1).get("role").asText());
        assertEquals("Hello", input.get(input.size() - 1).get("content").asText());

        assertEquals(0.3, requestJson.get("temperature").asDouble(), 0.001);
    }
}
