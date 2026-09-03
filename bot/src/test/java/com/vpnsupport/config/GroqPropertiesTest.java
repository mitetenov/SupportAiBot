package com.vpnsupport.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class GroqPropertiesTest {

    private final GroqProperties properties = new GroqProperties();

    @Test
    void shouldDefaultBaseUrlToGroqOpenAiCompatibleEndpoint() {
        assertEquals("https://api.groq.com/openai/v1", properties.getBaseUrl());
    }

    @Test
    void shouldDefaultModelToSupportedGroqModel() {
        assertEquals("llama-3.3-70b-versatile", properties.getModel());
    }

    @Test
    void shouldAllowConfiguringCredentialsAndModel() {
        properties.setApiKey("groq-test-key");
        properties.setModel("llama-3.3-70b-versatile");

        assertEquals("groq-test-key", properties.getApiKey());
        assertEquals("llama-3.3-70b-versatile", properties.getModel());
    }
}
