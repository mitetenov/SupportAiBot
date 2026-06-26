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

import static org.junit.jupiter.api.Assertions.assertTrue;

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
    void buildRequestBodyShouldIncludeCorrectTelegramId() throws Exception {
        long telegramUserId = 12345L;
        List<Map<String, Object>> contents = List.of();
        String faq = "FAQ context";

        var method = GeminiClient.class.getDeclaredMethod(
                "buildRequestBody", List.class, String.class, long.class);
        method.setAccessible(true);
        ObjectNode body = (ObjectNode) method.invoke(client, contents, faq, telegramUserId);

        String systemText = body.get("system_instruction")
                .get("parts").get(0).get("text").asText();

        assertTrue(systemText.contains("Telegram ID: 12345"),
                "System prompt must contain Telegram ID: " + telegramUserId + ", but got: " + systemText);
        assertTrue(systemText.contains("FAQ context"));
    }

    @Test
    void buildRequestBodyShouldNotContainDefaultTelegramId() throws Exception {
        long telegramUserId = 777L;
        List<Map<String, Object>> contents = List.of();

        var method = GeminiClient.class.getDeclaredMethod(
                "buildRequestBody", List.class, String.class, long.class);
        method.setAccessible(true);
        ObjectNode body = (ObjectNode) method.invoke(client, contents, null, telegramUserId);

        String systemText = body.get("system_instruction")
                .get("parts").get(0).get("text").asText();

        assertTrue(systemText.contains("Telegram ID: 777"),
                "System prompt must contain the actual user ID 777, but got: " + systemText);
        assertTrue(!systemText.contains("Telegram ID: 0"),
                "System prompt must NOT contain Telegram ID: 0 when real ID is different");
    }
}
