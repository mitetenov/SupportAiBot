package com.vpnsupport.rag;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.io.IOException;
import java.io.InputStream;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Guards the FAQ content itself. The bot copies these answers verbatim, so a
 * wrong section name or a missing keyword reaches users directly — and none of
 * the code-level tests would notice.
 */
class FaqContentTest {

    private static final String CONNECTION_TAB = "«Подключиться»";
    private static final String CABINET_SECTION = "«Подключить устройство»";

    private static List<Map<String, Object>> faq() throws IOException {
        try (InputStream is = FaqContentTest.class.getResourceAsStream("/faq/faq.json")) {
            return new ObjectMapper().readValue(is, new TypeReference<>() {
            });
        }
    }

    static Stream<Map<String, Object>> entries() throws IOException {
        return faq().stream();
    }

    @ParameterizedTest
    @MethodSource("entries")
    void everyEntryShouldHaveAQuestionAnswerAndKeywords(Map<String, Object> entry) {
        assertFalse(String.valueOf(entry.get("question")).isBlank());
        assertFalse(String.valueOf(entry.get("answer")).isBlank());
        assertTrue(entry.get("keywords") instanceof List<?> k && !k.isEmpty(),
                "entry has no keywords: " + entry.get("question"));
    }

    /**
     * The setup instruction is the single place that knows how to install on
     * every platform. An answer that points at the cabinet for connecting a
     * device has to name the section, or the user lands on a dashboard and has
     * to hunt.
     */
    @ParameterizedTest
    @MethodSource("entries")
    void anAnswerNamingTheConnectionTabShouldAlsoNameTheCabinetSection(Map<String, Object> entry) {
        String answer = String.valueOf(entry.get("answer"));
        if (answer.contains(CONNECTION_TAB) && answer.contains("lk.peipivo.top")) {
            assertTrue(answer.contains(CABINET_SECTION),
                    "names the bot tab but not the cabinet section: " + entry.get("question"));
        }
    }

    @ParameterizedTest
    @MethodSource("entries")
    void noAnswerShouldRepeatTheCabinetSection(Map<String, Object> entry) {
        String answer = String.valueOf(entry.get("answer"));
        int occurrences = answer.split(CABINET_SECTION, -1).length - 1;
        assertTrue(occurrences <= 1,
                "cabinet section named twice: " + entry.get("question"));
    }

    /**
     * Users ask this in several ways. The question text carries the most weight
     * in retrieval, so all of these have to appear somewhere findable.
     */
    @Test
    void theSetupEntryShouldCoverTheWaysUsersAskAboutInstalling() throws IOException {
        Map<String, Object> setup = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).startsWith("Как установить"))
                .findFirst()
                .orElseThrow(() -> new AssertionError("no installation entry in the FAQ"));

        String haystack = (setup.get("question") + " " + setup.get("keywords")).toLowerCase();
        for (String phrasing : List.of("установить", "настроить", "подключить", "скачать",
                "iphone", "android", "windows", "mac", "linux")) {
            assertTrue(haystack.contains(phrasing),
                    "installation entry does not mention '" + phrasing + "'");
        }

        String answer = String.valueOf(setup.get("answer"));
        assertTrue(answer.contains("@PeipivoSalesBot") && answer.contains(CONNECTION_TAB));
        assertTrue(answer.contains(CABINET_SECTION));
    }

    /**
     * Postgres full-text search ANDs the terms of a query, so one unmatched
     * word sinks the whole thing. Measured against the real corpus, every
     * phrasing below returned zero entries because the device name was only
     * stored in Latin while users type Cyrillic.
     */
    @Test
    void theSetupEntryShouldCarryCyrillicSpellingsOfDeviceNames() throws IOException {
        String keywords = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).startsWith("Как установить"))
                .map(e -> String.valueOf(e.get("keywords")).toLowerCase())
                .findFirst()
                .orElseThrow();

        for (String cyrillic : List.of("макбук", "мак", "виндовс", "линукс", "убунту",
                "айфон", "андроид", "планшет", "ноутбук", "пк", "телефон")) {
            assertTrue(keywords.contains(cyrillic),
                    "users type '" + cyrillic + "' but the entry only has the Latin spelling");
        }
    }

    /**
     * Connecting a device is the same job whether it is the first one or the
     * fourth, so both entries must route to the same instruction. The
     * additional-device entry also has to be findable from how people phrase
     * it — "настроить впн на втором телефоне" matched nothing until it carried
     * both the verb and the device names.
     */
    @Test
    void theAdditionalDeviceEntryShouldRouteToTheSameInstruction() throws IOException {
        Map<String, Object> entry = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).startsWith("Как подключить еще одно устройство"))
                .findFirst()
                .orElseThrow(() -> new AssertionError("no additional-device entry in the FAQ"));

        String answer = String.valueOf(entry.get("answer"));
        assertTrue(answer.contains("@PeipivoSalesBot") && answer.contains(CONNECTION_TAB));
        assertTrue(answer.contains(CABINET_SECTION));
        assertTrue(answer.contains("тем же аккаунтом"),
                "a returning user must be told not to create a new account");

        String keywords = String.valueOf(entry.get("keywords")).toLowerCase();
        for (String phrasing : List.of("настроить", "подключить", "второе устройство",
                "телефон", "ноутбук", "пк")) {
            assertTrue(keywords.contains(phrasing),
                    "additional-device entry does not mention '" + phrasing + "'");
        }
    }

    /** The general entry must not read as first-time-only either. */
    @Test
    void theSetupEntryShouldAlsoCoverAddingAnotherDevice() throws IOException {
        String answer = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).startsWith("Как установить"))
                .map(e -> String.valueOf(e.get("answer")))
                .findFirst()
                .orElseThrow();

        assertTrue(answer.contains("ещё одно устройство"));
        assertTrue(answer.contains("тем же аккаунтом"));
    }

    /**
     * The whole point of routing to the ready-made instruction is that the bot
     * does not describe the steps itself.
     */
    @Test
    void theSetupEntryShouldNotSpellOutInstallationSteps() throws IOException {
        String answer = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).startsWith("Как установить"))
                .map(e -> String.valueOf(e.get("answer")).toLowerCase())
                .findFirst()
                .orElseThrow();

        for (String improvised : List.of("app store", "google play", "apk",
                "вставьте ссылку", "добавьте подписку", "скопируйте ссылку")) {
            assertFalse(answer.contains(improvised),
                    "the answer walks the user through installation instead of linking it: "
                            + improvised);
        }
    }

    @Test
    void theNaServersEntryShouldCoverHappIncompatibilityAndIncySolution() throws IOException {
        Map<String, Object> entry = faq().stream()
                .filter(e -> String.valueOf(e.get("question")).contains("n/a"))
                .findFirst()
                .orElseThrow(() -> new AssertionError("no n/a servers entry in the FAQ"));

        String answer = String.valueOf(entry.get("answer"));
        assertTrue(answer.toLowerCase().contains("happ"));
        assertTrue(answer.toLowerCase().contains("incy"));
        assertTrue(answer.contains("@PeipivoSalesBot") && answer.contains(CONNECTION_TAB));
        assertTrue(answer.contains(CABINET_SECTION));

        String haystack = (entry.get("question") + " " + entry.get("keywords")).toLowerCase();
        for (String phrasing : List.of("n/a", "na", "happ", "incy", "обновлен")) {
            assertTrue(haystack.contains(phrasing),
                    "n/a servers entry does not mention '" + phrasing + "'");
        }
    }
}
