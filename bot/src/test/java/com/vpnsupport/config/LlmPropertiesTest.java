package com.vpnsupport.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class LlmPropertiesTest {

    private final LlmProperties properties = new LlmProperties();

    @Test
    void shouldDefaultToDeepseek() {
        assertEquals("deepseek", properties.getProvider());
    }

    @Test
    void shouldAllowChangingProvider() {
        properties.setProvider("gemini");
        assertEquals("gemini", properties.getProvider());
    }

    @Test
    void shouldAllowSettingProviderToNull() {
        properties.setProvider(null);
        assertEquals(null, properties.getProvider());
    }
}
