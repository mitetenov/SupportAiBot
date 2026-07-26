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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.when;

/**
 * Covers {@link AbstractLlmClient#buildContextualSearchQuery}: deciding when a
 * message needs the previous turn prefixed before it can be searched.
 */
@ExtendWith(MockitoExtension.class)
class ContextualSearchQueryTest {

    private static final long USER_ID = 1L;
    private static final String PREVIOUS = "не работает впн на телефоне";

    @Mock private McpRouter mcpRouter;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private FaqEmbeddingService faqEmbeddingService;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;

    private DeepSeekClient client;

    @BeforeEach
    void setUp() {
        DeepSeekProperties properties = new DeepSeekProperties();
        properties.setBaseUrl("http://localhost:9999");
        properties.setModel("m");
        properties.setApiKey("k");
        client = new DeepSeekClient(properties, new ObjectMapper(), mcpRouter,
                chatHistoryService, faqEmbeddingService, tokenUsageRepository);
        lenient().when(chatHistoryService.getLastUserMessage(USER_ID)).thenReturn(PREVIOUS);
    }

    private String query(String message) {
        return client.buildContextualSearchQuery(USER_ID, message);
    }

    @Test
    void shouldPrefixContextForAContinuationParticle() {
        assertEquals(PREVIOUS + " а на айфоне?", query("а на айфоне?"));
    }

    @Test
    void shouldPrefixContextForABarePlatformName() {
        assertEquals(PREVIOUS + " айфон", query("айфон"));
    }

    @Test
    void shouldPrefixContextForAnAnaphoricReference() {
        assertEquals(PREVIOUS + " это не помогло", query("это не помогло"));
    }

    @Test
    void shouldPrefixContextForABareAcknowledgement() {
        assertEquals(PREVIOUS + " да", query("да"));
    }

    @Test
    void shouldPrefixContextForAMessageWithNoLetters() {
        assertEquals(PREVIOUS + " ???", query("???"));
    }

    /**
     * The heart of the fix: a short but self-contained question must be searched
     * on its own. The old rule keyed on length (< 35 characters) and dragged
     * unrelated context into every brief question.
     */
    @Test
    void shouldNotPrefixContextForAShortSelfContainedQuestion() {
        assertEquals("Как оплатить?", query("Как оплатить?"));
    }

    @Test
    void shouldNotPrefixContextForOtherShortTopicChanges() {
        assertEquals("Где мой QR-код", query("Где мой QR-код"));
        assertEquals("Сколько стоит?", query("Сколько стоит?"));
        assertEquals("Верните деньги", query("Верните деньги"));
    }

    @Test
    void shouldNotPrefixContextForALongMessage() {
        String message = "У меня перестал работать интернет после обновления приложения, "
                + "и я не понимаю, что делать дальше";
        assertEquals(message, query(message));
    }

    @Test
    void shouldReturnTheMessageWhenThereIsNoHistory() {
        when(chatHistoryService.getLastUserMessage(USER_ID)).thenReturn(null);
        assertEquals("айфон", query("айфон"));
    }

    @Test
    void shouldNotDuplicateAnIdenticalRepeatedMessage() {
        when(chatHistoryService.getLastUserMessage(USER_ID)).thenReturn("айфон");
        assertEquals("айфон", query("айфон"));
    }

    @Test
    void shouldPassThroughEmptyInput() {
        assertEquals("", query(""));
        assertEquals(null, query(null));
    }
}
