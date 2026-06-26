package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullAndEmptySource;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

class TelegramMessageSenderTest {

    @ParameterizedTest
    @NullAndEmptySource
    void splitShouldReturnEmptyStringListForNullOrEmpty(String text) {
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals("", result.get(0));
    }

    @Test
    void splitShouldReturnSingleElementForShortText() {
        String text = "Hello, world!";
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals(text, result.get(0));
    }

    @Test
    void splitShouldReturnSingleElementForExactMaxLength() {
        String text = "A".repeat(4096);
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals(text, result.get(0));
    }

    @Test
    void splitShouldBreakAtNewlineWhenOverMaxLength() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4000, result.get(0).length());
        assertEquals(200, result.get(1).length());
    }

    @Test
    void splitShouldHardBreakWhenNoNewline() {
        String text = "A".repeat(5000);
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4096, result.get(0).length());
        assertEquals(904, result.get(1).length());
    }

    @Test
    void splitShouldSkipLeadingNewlineBetweenChunks() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n\n\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4002, result.get(0).length());
        assertFalse(result.get(1).startsWith("\n"));
    }

    @Test
    void splitShouldHandleVeryLongText() {
        String text = "B".repeat(15000);
        List<String> result = TelegramMessageSender.split(text);
        int expectedChunks = (int) Math.ceil(15000.0 / 4096.0);
        assertEquals(expectedChunks, result.size());
        int totalLength = result.stream().mapToInt(String::length).sum();
        assertEquals(15000, totalLength);
    }

    @Test
    void splitShouldHandleTextWithTrailingNewline() {
        String text = "Hello\nWorld\n";
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals("Hello\nWorld\n", result.get(0));
    }

    @Test
    void splitShouldBreakOnLastNewlineWithinLimit() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
    }

    @Test
    void splitShouldHandleTextWithNewlineAtMaxBoundary() {
        String part1 = "A".repeat(4095) + "\n";
        String part2 = "B".repeat(100);
        String text = part1 + part2;

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4095, result.get(0).length());
        assertEquals(100, result.get(1).length());
    }
}
