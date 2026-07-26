package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * The one thing worth pinning down: the technical message and the message the
 * user sees are separate, and the technical one never leaks to the user.
 */
class LlmProcessingExceptionTest {

    @Test
    void shouldKeepTheTechnicalAndUserFacingMessagesApart() {
        LlmProcessingException ex = new LlmProcessingException(
                "API error: timeout after 60s at api.deepseek.com",
                "Произошла ошибка при обработке запроса. Попробуйте позже.");

        assertEquals("API error: timeout after 60s at api.deepseek.com", ex.getMessage());
        assertEquals("Произошла ошибка при обработке запроса. Попробуйте позже.",
                ex.getUserFriendlyMessage());
    }

    @Test
    void shouldPreserveTheCauseForLogging() {
        Throwable cause = new RuntimeException("Connection refused");
        LlmProcessingException ex = new LlmProcessingException("LLM failed", "Ошибка", cause);

        assertEquals(cause, ex.getCause());
    }

    @Test
    void shouldTolerateAMissingUserFacingMessage() {
        assertNull(new LlmProcessingException("Error", null).getUserFriendlyMessage());
    }
}
