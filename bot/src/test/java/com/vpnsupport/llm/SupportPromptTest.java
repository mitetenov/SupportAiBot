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
        assertTrue(SupportPrompt.SYSTEM.contains("никогда не объясняй сам"),
                "the prompt must forbid improvising installation steps");
        assertTrue(SupportPrompt.SYSTEM.contains("«Подключиться»"),
                "the prompt must name the tab in @PeipivoSalesBot");
        assertTrue(SupportPrompt.SYSTEM.contains("«Подключить устройство»"),
                "the prompt must name the section in the cabinet");
    }

    /**
     * Scoping the rule to "first connection" invited the model to decide the
     * rule did not apply to somebody adding a second device — and improvise the
     * steps again. Connecting a device is the same job either way.
     */
    @Test
    void theConnectionRuleShouldCoverAdditionalDevicesToo() {
        assertTrue(SupportPrompt.SYSTEM.contains("ПОДКЛЮЧЕНИЕ УСТРОЙСТВА"));
        assertTrue(SupportPrompt.SYSTEM.contains("ПЕРВОГО устройства и для ЛЮБОГО СЛЕДУЮЩЕГО"));
        assertTrue(SupportPrompt.SYSTEM.contains("под тем же аккаунтом"),
                "a returning user must not be sent to create a new account");
    }

    @Test
    void systemShouldStillAllowTroubleshootingForAlreadyConnectedUsers() {
        // The connection rule covers any device, but it must not swallow
        // diagnostics: a connected user with a problem goes to section Б.
        assertTrue(SupportPrompt.SYSTEM.contains("Правило не отменяет диагностику"));
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

    @Test
    void systemShouldMentionHappIncompatibilityAndIncySolution() {
        assertTrue(SupportPrompt.SYSTEM.contains("Happ") && SupportPrompt.SYSTEM.contains("Incy"));
        assertTrue(SupportPrompt.SYSTEM.contains("n/a"));
        assertTrue(SupportPrompt.SYSTEM.contains("несовместим"));
    }
}
