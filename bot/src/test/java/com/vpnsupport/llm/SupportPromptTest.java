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

    /**
     * Without this rule the model improvises installation steps from the product
     * constraints — "download Happ and paste the subscription link" — whenever
     * retrieval fails to surface the setup entry. The ready-made instruction
     * detects the platform itself, so it must always win.
     */
    @Test
    void systemShouldSendInstallationQuestionsToTheReadyMadeInstruction() {
        assertTrue(SupportPrompt.SYSTEM.contains("УСТАНОВКА И ПЕРВОЕ ПОДКЛЮЧЕНИЕ"),
                "the prompt must forbid improvising installation steps");
        assertTrue(SupportPrompt.SYSTEM.contains("«Подключиться»"),
                "the prompt must name the tab in @PeipivoSalesBot");
        assertTrue(SupportPrompt.SYSTEM.contains("«Подключить устройство»"),
                "the prompt must name the section in the cabinet");
    }

    @Test
    void systemShouldStillAllowTroubleshootingForAlreadyConnectedUsers() {
        // The installation rule is scoped: a user who is connected and has a
        // problem still goes through the diagnostics section.
        assertTrue(SupportPrompt.SYSTEM.contains(
                "Это касается только установки и первого подключения"));
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
