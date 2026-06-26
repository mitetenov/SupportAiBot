package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class LlmProcessingExceptionTest {

    @Test
    void shouldStoreMessageAndUserFriendlyMessage() {
        LlmProcessingException ex = new LlmProcessingException(
                "API error: timeout",
                "Извините, произошла ошибка. Попробуйте позже."
        );
        assertEquals("API error: timeout", ex.getMessage());
        assertEquals("Извините, произошла ошибка. Попробуйте позже.", ex.getUserFriendlyMessage());
    }

    @Test
    void shouldStoreCause() {
        Throwable cause = new RuntimeException("Connection refused");
        LlmProcessingException ex = new LlmProcessingException(
                "LLM failed",
                "Ошибка обработки запроса",
                cause
        );
        assertEquals("LLM failed", ex.getMessage());
        assertEquals("Ошибка обработки запроса", ex.getUserFriendlyMessage());
        assertNotNull(ex.getCause());
        assertEquals("Connection refused", ex.getCause().getMessage());
    }

    @Test
    void shouldAllowNullUserFriendlyMessage() {
        LlmProcessingException ex = new LlmProcessingException("Error", null);
        assertEquals("Error", ex.getMessage());
        assertNull(ex.getUserFriendlyMessage());
    }

    @Test
    void shouldAllowNullCause() {
        LlmProcessingException ex = new LlmProcessingException("Error", "User msg", null);
        assertEquals("Error", ex.getMessage());
        assertEquals("User msg", ex.getUserFriendlyMessage());
        assertNull(ex.getCause());
    }

    @Test
    void shouldExtendRuntimeException() {
        LlmProcessingException ex = new LlmProcessingException("err", "user");
        boolean isRuntimeException = ex instanceof RuntimeException;
        if (!isRuntimeException) {
            throw new AssertionError("Expected RuntimeException subclass");
        }
    }
}
