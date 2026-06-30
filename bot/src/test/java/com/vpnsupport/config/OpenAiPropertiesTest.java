package com.vpnsupport.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

class OpenAiPropertiesTest {

    private final OpenAiProperties properties = new OpenAiProperties();

    @Test
    void shouldDefaultBaseUrl() {
        assertEquals("https://api.openai.com/v1", properties.getBaseUrl());
    }

    @Test
    void shouldDefaultModelToNull() {
        assertNull(properties.getModel());
    }

    @Test
    void shouldAllowChangingBaseUrl() {
        properties.setBaseUrl("https://custom.openai.com/v1");
        assertEquals("https://custom.openai.com/v1", properties.getBaseUrl());
    }

    @Test
    void shouldAllowChangingModel() {
        properties.setModel("gpt-4o");
        assertEquals("gpt-4o", properties.getModel());
    }

    @Test
    void shouldAllowSettingApiKey() {
        properties.setApiKey("sk-test123");
        assertEquals("sk-test123", properties.getApiKey());
    }

    @Test
    void shouldAllowSettingApiKeyToNull() {
        properties.setApiKey("something");
        properties.setApiKey(null);
        assertNull(properties.getApiKey());
    }
}
