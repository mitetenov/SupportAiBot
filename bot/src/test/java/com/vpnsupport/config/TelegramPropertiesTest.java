package com.vpnsupport.config;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TelegramPropertiesTest {

    private final TelegramProperties properties = new TelegramProperties();

    @ParameterizedTest
    @NullAndEmptySource
    @ValueSource(strings = {"  ", "\t", "\n"})
    void shouldReturnEmptySetForBlankAdminIds(String input) {
        properties.setSupportAdminTelegramIds(input);
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertTrue(ids.isEmpty());
    }

    @Test
    void shouldParseSingleAdminId() {
        properties.setSupportAdminTelegramIds("12345");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertEquals(1, ids.size());
        assertTrue(ids.contains(12345L));
    }

    @Test
    void shouldParseMultipleAdminIds() {
        properties.setSupportAdminTelegramIds("12345,67890,11111");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertEquals(3, ids.size());
        assertTrue(ids.contains(12345L));
        assertTrue(ids.contains(67890L));
        assertTrue(ids.contains(11111L));
    }

    @Test
    void shouldHandleWhitespaceInAdminIds() {
        properties.setSupportAdminTelegramIds(" 12345 , 67890 , 11111 ");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertEquals(3, ids.size());
        assertTrue(ids.contains(12345L));
        assertTrue(ids.contains(67890L));
        assertTrue(ids.contains(11111L));
    }

    @Test
    void shouldFilterEmptySegments() {
        properties.setSupportAdminTelegramIds("12345,,67890,");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertEquals(2, ids.size());
    }

    @Test
    void shouldSkipNonNumericAdminId() {
        properties.setSupportAdminTelegramIds("abc");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertTrue(ids.isEmpty());
    }

    @Test
    void shouldSkipInvalidAndKeepValidAdminIds() {
        properties.setSupportAdminTelegramIds("12345,abc,67890");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertEquals(2, ids.size());
        assertTrue(ids.contains(12345L));
        assertTrue(ids.contains(67890L));
    }

    @Test
    void shouldReturnUnmodifiableSet() {
        properties.setSupportAdminTelegramIds("12345");
        Set<Long> ids = properties.getSupportAdminTelegramIds();
        assertThrows(UnsupportedOperationException.class, () -> ids.add(99999L));
    }
}
