package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RejectionDetectorTest {

    @Test
    void shouldRecogniseRejections() {
        assertTrue(RejectionDetector.isRejection("это не то"));
        assertTrue(RejectionDetector.isRejection("не подходит"));
        assertTrue(RejectionDetector.isRejection("не помогло"));
        assertTrue(RejectionDetector.isRejection("дайте другой вариант"));
        assertTrue(RejectionDetector.isRejection("нет, я про другое"));
    }

    @Test
    void shouldNotRecogniseOrdinaryQuestions() {
        assertFalse(RejectionDetector.isRejection("как оплатить подписку"));
        assertFalse(RejectionDetector.isRejection("не работает VPN"));
        assertFalse(RejectionDetector.isRejection(null));
        assertFalse(RejectionDetector.isRejection("  "));
    }

    /**
     * The retriever and the history service must agree: when they disagreed, a
     * rejection one recognised and the other did not cleared the exclusion list
     * and the user was handed the instruction they had just rejected.
     */
    @Test
    void shouldAgreeAcrossBothCallSites() {
        for (String rejection : new String[]{"нет, не то", "не та инструкция", "другое"}) {
            assertTrue(RejectionDetector.isRejection(rejection), rejection);
        }
    }
}
