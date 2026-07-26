package com.vpnsupport.bot;

import java.util.List;

/**
 * Recognises a user turning down the answer they were just given
 * ("это не то", "не подходит", …).
 *
 * <p>Two call sites depend on agreeing about this: the retriever re-searches
 * against the <em>previous</em> question on a rejection, and the history service
 * keeps the already-shown FAQ entries excluded instead of resetting them. When
 * the two disagreed, a rejection could clear the exclusion list and the user got
 * the very instruction they had just rejected.
 */
public final class RejectionDetector {

    private static final List<String> REJECTION_PHRASES = List.of(
            "не то",
            "не та",
            "не это",
            "не подходит",
            "не помог",
            "другой вариант",
            "другая инструкция",
            "другое",
            "нет,"
    );

    public static boolean isRejection(String message) {
        if (message == null || message.isBlank()) {
            return false;
        }
        String lower = message.toLowerCase();
        return REJECTION_PHRASES.stream().anyMatch(lower::contains);
    }

    private RejectionDetector() {
    }
}
