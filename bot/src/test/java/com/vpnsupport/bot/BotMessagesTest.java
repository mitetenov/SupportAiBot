package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.context.MessageSource;
import org.springframework.context.support.ResourceBundleMessageSource;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Guards the contract between the code and {@code messages.properties}.
 *
 * <p>A key present in code but missing from the bundle throws
 * {@link org.springframework.context.NoSuchMessageException} at runtime — and
 * several of these keys are used from error handlers, so the bot would fail
 * while reporting a failure. Scanning the sources keeps that impossible without
 * anyone having to remember.
 */
class BotMessagesTest {

    private static final Path SOURCE_ROOT = Path.of("src/main/java/com/vpnsupport");
    private static final Path BUNDLE = Path.of("src/main/resources/messages.properties");

    /** {@code messages.get("some.key"} and {@code Result.failed("some.key")}. */
    private static final Pattern KEY_REFERENCE = Pattern.compile(
            "(?:messages\\.get|Result\\.failed|failed)\\(\\s*\"([a-z][a-z0-9._]*)\"");

    private static final BotMessages MESSAGES = new BotMessages(realMessageSource());

    private static MessageSource realMessageSource() {
        ResourceBundleMessageSource source = new ResourceBundleMessageSource();
        source.setBasename("messages");
        source.setDefaultEncoding(StandardCharsets.UTF_8.name());
        return source;
    }

    /** Every message key referenced anywhere in main sources. */
    static Stream<String> referencedKeys() throws IOException {
        Set<String> keys = new LinkedHashSet<>();
        try (var paths = Files.walk(SOURCE_ROOT)) {
            for (Path file : paths.filter(p -> p.toString().endsWith(".java")).toList()) {
                Matcher matcher = KEY_REFERENCE.matcher(Files.readString(file));
                while (matcher.find()) {
                    keys.add(matcher.group(1));
                }
            }
        }
        return keys.stream();
    }

    @Test
    void shouldFindTheKeysItIsSupposedToCheck() {
        // Guards the scanner itself: a regex that silently matches nothing would
        // make every assertion below vacuously pass.
        List<String> keys = assertDoesNotThrow(() -> referencedKeys().toList());
        assertTrue(keys.size() > 15, "expected the scan to find the bot's message keys, got " + keys);
        assertTrue(keys.contains("bot.llm.error"));
        assertTrue(keys.contains("bot.media.unsupported"));
    }

    @ParameterizedTest
    @MethodSource("referencedKeys")
    void everyKeyUsedInCodeShouldResolve(String key) {
        String resolved = assertDoesNotThrow(() -> MESSAGES.get(key),
                "missing key in messages.properties: " + key);
        assertNotNull(resolved);
        assertFalse(resolved.isBlank(), "key resolves to a blank string: " + key);
    }

    @Test
    void shouldSubstitutePositionalArguments() {
        assertEquals("Поддержка: привет", MESSAGES.get("support.operator.prefix", "привет"));
    }

    @Test
    void shouldSubstituteEveryArgumentOfTheStatsMessage() {
        String text = MESSAGES.get("bot.stats.user", "@johndoe", 5L, "1.0K", "500", "1.5K");

        assertTrue(text.contains("@johndoe"));
        assertTrue(text.contains("1.5K"));
        assertFalse(text.contains("{"), "an unsubstituted placeholder remained: " + text);
    }

    @Test
    void shouldNotShipUnusedKeys() throws IOException {
        Set<String> declared = new LinkedHashSet<>();
        for (String line : Files.readAllLines(BUNDLE)) {
            String trimmed = line.trim();
            if (!trimmed.isEmpty() && !trimmed.startsWith("#") && trimmed.contains("=")) {
                declared.add(trimmed.substring(0, trimmed.indexOf('=')).trim());
            }
        }
        declared.removeAll(referencedKeys().toList());

        assertTrue(declared.isEmpty(),
                "messages.properties declares keys nothing uses: " + declared);
    }
}
