package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.GroqProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.web.reactive.function.client.WebClientRequestException;

import java.io.IOException;
import java.io.OutputStream;
import java.net.URI;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeoutException;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class GroqClientIntegrationTest {

    @Mock
    private McpRouter mcpRouter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

    private HttpServer mockServer;
    private GroqClient client;
    private ObjectMapper objectMapper;
    private volatile String capturedRequestBody;
    private volatile String capturedAuthorization;
    private volatile String responseToReturn;
    private volatile int responseStatusCode;
    private List<String> capturedRequestBodies;
    private List<StubbedResponse> responseSequence;
    private int requestCount;

    @BeforeEach
    void setUp() throws Exception {
        objectMapper = new ObjectMapper();
        capturedRequestBody = null;
        capturedAuthorization = null;
        responseStatusCode = 200;
        responseToReturn = "{}";
        capturedRequestBodies = new java.util.ArrayList<>();
        responseSequence = List.of();
        requestCount = 0;

        mockServer = HttpServer.create(new InetSocketAddress(0), 0);
        mockServer.createContext("/chat/completions", this::handleChatCompletions);
        mockServer.start();

        GroqProperties properties = new GroqProperties();
        properties.setBaseUrl("http://localhost:" + mockServer.getAddress().getPort());
        properties.setApiKey("groq-test-key");
        properties.setModel("llama-3.3-70b-versatile");

        client = new GroqClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
    }

    @AfterEach
    void tearDown() {
        if (mockServer != null) {
            mockServer.stop(0);
        }
    }

    @Test
    void shouldSendSelectedModelAndBearerCredentialToGroq() throws Exception {
        responseToReturn = """
                {
                    "choices": [{"message": {"content": "Groq response"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                }
                """;
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
        when(mcpRouter.listTools()).thenReturn(List.of());

        String response = client.callApi(client.buildInitialConversation("Hello", 123L, null, null, null), null, 123L);

        assertEquals("Groq response", objectMapper.readTree(response).at("/choices/0/message/content").asText());
        assertEquals("Bearer groq-test-key", capturedAuthorization);
        assertNotNull(capturedRequestBody);
        assertEquals("llama-3.3-70b-versatile", objectMapper.readTree(capturedRequestBody).get("model").asText());
    }

    @Test
    void shouldMapGroqRateLimitFailureWithoutExposingApiResponse() {
        responseStatusCode = 429;
        responseToReturn = "{\"error\": {\"message\": \"Rate limit exceeded\"}}";
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
        when(mcpRouter.listTools()).thenReturn(List.of());

        LlmProcessingException error = assertThrows(LlmProcessingException.class, () ->
                client.callApi(client.buildInitialConversation("Hello", 123L, null, null, null), null, 123L));

        assertEquals("Groq request failed with HTTP 429", error.getMessage());
        assertEquals("Превышен лимит запросов к Groq. Попробуйте позже.", error.getUserFriendlyMessage());
        assertFalse(error.getMessage().contains("Rate limit exceeded"));
    }

    @Test
    void shouldMapGroqAuthenticationFailureWithoutExposingApiResponse() {
        responseStatusCode = 401;
        responseToReturn = "{\"error\": {\"message\": \"invalid key: groq-secret\"}}";
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
        when(mcpRouter.listTools()).thenReturn(List.of());

        LlmProcessingException error = assertThrows(LlmProcessingException.class, () ->
                client.callApi(client.buildInitialConversation("Hello", 123L, null, null, null), null, 123L));

        assertEquals("Groq authentication failed with HTTP 401", error.getMessage());
        assertEquals("Не удалось авторизоваться в Groq. Проверьте GROQ_API_KEY.", error.getUserFriendlyMessage());
        assertFalse(error.getMessage().contains("groq-secret"));
    }

    @Test
    void shouldMapGenericGroqApiFailureWithoutExposingApiResponse() {
        responseStatusCode = 503;
        responseToReturn = "{\"error\": {\"message\": \"upstream details\"}}";
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
        when(mcpRouter.listTools()).thenReturn(List.of());

        LlmProcessingException error = assertThrows(LlmProcessingException.class, () ->
                client.callApi(client.buildInitialConversation("Hello", 123L, null, null, null), null, 123L));

        assertEquals("Groq request failed with HTTP 503", error.getMessage());
        assertEquals("Сервис Groq временно недоступен. Попробуйте позже.", error.getUserFriendlyMessage());
        assertFalse(error.getMessage().contains("upstream details"));
    }

    @Test
    void shouldMapNetworkFailureToSafeMessage() {
        mockServer.stop(0);
        mockServer = null;
        when(chatHistoryService.getHistory(anyLong())).thenReturn(List.of());
        when(mcpRouter.listTools()).thenReturn(List.of());

        LlmProcessingException error = assertThrows(LlmProcessingException.class, () ->
                client.callApi(client.buildInitialConversation("Hello", 123L, null, null, null), null, 123L));

        assertEquals("Groq network request failed", error.getMessage());
        assertEquals("Не удалось связаться с Groq. Проверьте подключение и попробуйте позже.",
                error.getUserFriendlyMessage());
    }

    @Test
    void shouldMapTimeoutToSafeMessage() throws Exception {
        WebClientRequestException requestException = new WebClientRequestException(
                new TimeoutException("simulated timeout"), HttpMethod.POST,
                URI.create("http://localhost/chat/completions"), new HttpHeaders());
        var method = GroqClient.class.getDeclaredMethod("toNetworkException", WebClientRequestException.class);
        method.setAccessible(true);

        LlmProcessingException error = (LlmProcessingException) method.invoke(client, requestException);

        assertEquals("Groq request timed out", error.getMessage());
        assertEquals("Groq не ответил вовремя. Попробуйте позже.", error.getUserFriendlyMessage());
    }

    @Test
    void shouldRejectBlankApiKey() {
        GroqProperties properties = new GroqProperties();
        properties.setBaseUrl("http://localhost:9999");
        properties.setApiKey("  ");
        properties.setModel("llama-3.3-70b-versatile");

        assertThrows(IllegalArgumentException.class, () -> new GroqClient(properties, objectMapper, mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository));
    }

    @Test
    void shouldNotSupportImagesForDefaultTextModel() {
        assertFalse(client.supportsImages());
    }

    @Test
    void shouldCompleteToolCallRoundTripUsingOpenAiCompatibleArgumentsFormat() throws Exception {
        responseSequence = List.of(
                new StubbedResponse(200, """
                        {"choices": [{"message": {"content": null, "tool_calls": [{
                          "id": "call_1", "type": "function",
                          "function": {"name": "nodes_list", "arguments": "{}"}
                        }]}}]}
                        """),
                new StubbedResponse(200, """
                        {"choices": [{"message": {"content": "There are no available nodes."}}]}
                        """)
        );
        when(chatHistoryService.getHistory(123L)).thenReturn(List.of());
        when(faqEmbeddingService.buildFaqContext("List available nodes")).thenReturn("");
        when(mcpRouter.listTools()).thenReturn(List.of(
                new McpTool("nodes_list", "List available nodes", Map.of("type", "object", "properties", Map.of()))
        ));
        when(mcpRouter.callTool(eq("nodes_list"), eq(Map.of()))).thenReturn("{\"nodes\": []}");

        String reply = client.chat("List available nodes", 123L);

        assertEquals("There are no available nodes.", reply);
        assertEquals(2, capturedRequestBodies.size());
        assertEquals("groq-test-key", capturedAuthorization.substring("Bearer ".length()));

        var firstRequest = objectMapper.readTree(capturedRequestBodies.get(0));
        assertEquals("llama-3.3-70b-versatile", firstRequest.get("model").asText());
        assertEquals("nodes_list", firstRequest.at("/tools/0/function/name").asText());
        assertEquals("auto", firstRequest.get("tool_choice").asText());

        var secondRequest = objectMapper.readTree(capturedRequestBodies.get(1));
        assertEquals("assistant", secondRequest.at("/messages/3/role").asText());
        assertTrue(secondRequest.at("/messages/3/tool_calls/0/function/arguments").isTextual());
        assertEquals("{}", secondRequest.at("/messages/3/tool_calls/0/function/arguments").asText());
        assertEquals("tool", secondRequest.at("/messages/4/role").asText());
        assertEquals("call_1", secondRequest.at("/messages/4/tool_call_id").asText());
        assertEquals("{\"nodes\": []}", secondRequest.at("/messages/4/content").asText());
        verify(mcpRouter).callTool("nodes_list", Map.of());
    }

    private void handleChatCompletions(HttpExchange exchange) throws IOException {
        capturedAuthorization = exchange.getRequestHeaders().getFirst("Authorization");
        capturedRequestBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        capturedRequestBodies.add(capturedRequestBody);
        StubbedResponse response = requestCount < responseSequence.size()
                ? responseSequence.get(requestCount++)
                : new StubbedResponse(responseStatusCode, responseToReturn);
        byte[] responseBytes = response.body().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(response.statusCode(), responseBytes.length);
        try (OutputStream output = exchange.getResponseBody()) {
            output.write(responseBytes);
        }
    }

    private record StubbedResponse(int statusCode, String body) {
    }
}
