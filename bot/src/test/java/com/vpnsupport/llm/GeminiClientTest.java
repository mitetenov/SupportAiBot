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

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(MockitoExtension.class)
class GeminiClientTest {

    @Mock
    private McpRouter mcpRouter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

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
    void shouldSupportImages() {
        assertTrue(client.supportsImages());
    }

    @Test
    void buildRequestBodyShouldIncludeStaticSystemPrompt() throws Exception {
        List<Map<String, Object>> contents = List.of();

        var method = GeminiClient.class.getDeclaredMethod(
                "buildRequestBody", List.class, String.class, long.class);
        method.setAccessible(true);
        ObjectNode body = (ObjectNode) method.invoke(client, contents, "FAQ", 12345L);

        String systemText = body.get("system_instruction")
                .get("parts").get(0).get("text").asText();

        assertTrue(systemText.contains("Ты — техподдержка VPN-сервиса"),
                "System instruction must contain the static system prompt");
        assertTrue(!systemText.contains("Telegram ID: 12345"),
                "System instruction must NOT contain Telegram ID");
    }

    @Test
    void shouldBuildInitialConversationForText() throws Exception {
        var method = GeminiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "FAQ", null, null);

        assertTrue(!conv.isEmpty(), "Conversation should not be empty");
        Map<String, Object> last = conv.get(conv.size() - 1);
        assertTrue("user".equals(last.get("role")));
    }

    @Test
    void shouldBuildInitialConversationIncludeDynamicContext() throws Exception {
        var method = GeminiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 777L, "FAQ text", null, null);

        assertEquals("user", conv.get(0).get("role"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> firstParts = (List<Map<String, Object>>) conv.get(0).get("parts");
        String firstText = (String) firstParts.get(0).get("text");
        assertTrue(firstText.contains("Telegram ID: 777"));
        assertTrue(firstText.contains("FAQ text"));

        assertEquals("model", conv.get(1).get("role"));
    }

    @Test
    void shouldBuildInitialConversationWithoutFaqWhenNull() throws Exception {
        var method = GeminiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, null, null, null);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> firstParts = (List<Map<String, Object>>) conv.get(0).get("parts");
        String firstText = (String) firstParts.get(0).get("text");
        assertTrue(firstText.contains("Telegram ID: 123"));
        assertTrue(!firstText.contains("FAQ"));
    }

    @Test
    void shouldBuildInitialConversationWithoutFaqWhenEmpty() throws Exception {
        var method = GeminiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Hello", 123L, "", null, null);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> firstParts = (List<Map<String, Object>>) conv.get(0).get("parts");
        String firstText = (String) firstParts.get(0).get("text");
        assertTrue(firstText.contains("Telegram ID: 123"));
        assertTrue(!firstText.contains("\n\n"));
    }

    @Test
    void shouldBuildInitialConversationForImage() throws Exception {
        var method = GeminiClient.class.getDeclaredMethod(
                "buildInitialConversation", String.class, long.class, String.class, String.class, String.class);
        method.setAccessible(true);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> conv = (List<Map<String, Object>>) method.invoke(
                client, "Describe", 123L, "FAQ", "base64data", "image/png");

        assertTrue(!conv.isEmpty());
        Map<String, Object> last = conv.get(conv.size() - 1);
        assertTrue("user".equals(last.get("role")));
        @SuppressWarnings("unchecked")
        List<Object> parts = (List<Object>) last.get("parts");
        assertTrue(parts.size() == 2, "Should have text and image parts");
    }
}
