package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SupportPromptTest {

    @Test
    void systemShouldNotBeNull() {
        assertNotNull(SupportPrompt.SYSTEM);
    }

    @Test
    void systemShouldContainEscalateMarker() {
        assertTrue(SupportPrompt.SYSTEM.contains("[ESCALATE]"));
    }

    @Test
    void withTelegramUserIdShouldAppendId() {
        String result = SupportPrompt.withTelegramUserId(12345L);
        assertNotNull(result);
        assertTrue(result.contains("Telegram ID: 12345"));
        assertTrue(result.startsWith(SupportPrompt.SYSTEM));
    }

    @Test
    void withFaqContextShouldIncludeContextAndId() {
        String context = "FAQ: Как настроить VPN...";
        String result = SupportPrompt.withFaqContext(context, 67890L);
        assertNotNull(result);
        assertTrue(result.contains("FAQ: Как настроить VPN..."));
        assertTrue(result.contains("Telegram ID: 67890"));
    }

    @Test
    void withFaqContextNullShouldNotIncludeFaq() {
        String result = SupportPrompt.withFaqContext(null, 123L);
        assertNotNull(result);
        assertTrue(result.contains("Telegram ID: 123"));
    }

    @Test
    void withFaqContextEmptyShouldNotIncludeFaq() {
        String result = SupportPrompt.withFaqContext("", 456L);
        assertNotNull(result);
        assertTrue(result.contains("Telegram ID: 456"));
    }

    @Test
    void withFaqContextBlankShouldNotIncludeFaq() {
        String result = SupportPrompt.withFaqContext("   ", 789L);
        assertNotNull(result);
        assertTrue(result.contains("Telegram ID: 789"));
    }

    @Test
    void withTelegramUserIdShouldHandleNegativeId() {
        String result = SupportPrompt.withTelegramUserId(-1L);
        assertNotNull(result);
        assertTrue(result.contains("Telegram ID: -1"));
    }
}
