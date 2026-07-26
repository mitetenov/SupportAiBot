package com.vpnsupport.bot;

import com.vpnsupport.config.ConversationProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ConversationStateTest {

    private static final long USER_ID = 100L;

    private static ConversationState state(Duration suppression, Duration lastQueryTtl) {
        ConversationProperties properties = new ConversationProperties();
        properties.setOperatorSuppressionWindow(suppression);
        properties.setLastQueryTtl(lastQueryTtl);
        return new ConversationState(properties);
    }

    private static ConversationState state() {
        return state(Duration.ofMinutes(30), Duration.ofHours(6));
    }

    private static FaqEmbeddingService.FaqContext context() {
        return new FaqEmbeddingService.FaqContext(
                "FAQ...",
                List.of(new FaqEmbeddingService.FaqResult("Q", "A", 0.8, 0.02)),
                0.8, "Q");
    }

    // ------------------------------------------------------- operator handover

    @Test
    void shouldReportNoOperatorActivityForAnUnknownUser() {
        assertFalse(state().isOperatorRecentlyActive(USER_ID));
    }

    @Test
    void shouldSuppressTheAiRightAfterAnOperatorReplies() {
        ConversationState state = state();
        state.recordOperatorReply(USER_ID);

        assertTrue(state.isOperatorRecentlyActive(USER_ID));
    }

    @Test
    void shouldStopSuppressingOnceTheWindowElapses() {
        ConversationState state = state(Duration.ZERO, Duration.ofHours(6));
        state.recordOperatorReply(USER_ID);

        assertFalse(state.isOperatorRecentlyActive(USER_ID));
    }

    @Test
    void shouldTrackUsersIndependently() {
        ConversationState state = state();
        state.recordOperatorReply(USER_ID);

        assertTrue(state.isOperatorRecentlyActive(USER_ID));
        assertFalse(state.isOperatorRecentlyActive(200L));
    }

    // ------------------------------------------------------------- last query

    @Test
    void shouldRememberTheLastQueryWithItsRetrieval() {
        ConversationState state = state();
        FaqEmbeddingService.FaqContext context = context();

        state.recordQuery(USER_ID, "не работает впн", context);

        ConversationState.LastQuery last = state.lastQuery(USER_ID).orElseThrow();
        assertEquals("не работает впн", last.text());
        assertSame(context, last.faqContext());
    }

    @Test
    void shouldOverwriteTheLastQueryOnEachTurn() {
        ConversationState state = state();
        state.recordQuery(USER_ID, "первый", context());
        state.recordQuery(USER_ID, "второй", context());

        assertEquals("второй", state.lastQuery(USER_ID).orElseThrow().text());
    }

    @Test
    void shouldIgnoreABlankQuery() {
        ConversationState state = state();
        state.recordQuery(USER_ID, "  ", context());
        state.recordQuery(USER_ID, null, context());

        assertTrue(state.lastQuery(USER_ID).isEmpty());
    }

    @Test
    void shouldForgetTheLastQueryOnceItExpires() {
        ConversationState state = state(Duration.ofMinutes(30), Duration.ZERO);
        state.recordQuery(USER_ID, "давний вопрос", context());

        assertTrue(state.lastQuery(USER_ID).isEmpty());
    }

    @Test
    void shouldSubstituteAnEmptyRetrievalWhenNoneWasRecorded() {
        ConversationState state = state();
        state.recordQuery(USER_ID, "вопрос", null);

        assertSame(FaqEmbeddingService.FaqContext.EMPTY,
                state.lastQuery(USER_ID).orElseThrow().faqContextOrEmpty());
    }

    // ----------------------------------------------------------------- cleanup

    @Test
    void shouldDropEverythingForAUserOnClear() {
        ConversationState state = state();
        state.recordQuery(USER_ID, "вопрос", context());
        state.recordOperatorReply(USER_ID);

        state.clear(USER_ID);

        assertTrue(state.lastQuery(USER_ID).isEmpty());
        assertFalse(state.isOperatorRecentlyActive(USER_ID));
    }

    /**
     * The scheduled sweep is what keeps these maps from holding one entry per
     * user who ever wrote in, for the lifetime of the process.
     */
    @Test
    void shouldEvictExpiredEntriesOnTheScheduledSweep() {
        ConversationState state = state(Duration.ZERO, Duration.ZERO);
        for (long id = 1; id <= 50; id++) {
            state.recordQuery(id, "вопрос " + id, context());
            state.recordOperatorReply(id);
        }

        state.evictExpired();

        for (long id = 1; id <= 50; id++) {
            assertTrue(state.lastQuery(id).isEmpty(), "user " + id + " should have been evicted");
            assertFalse(state.isOperatorRecentlyActive(id));
        }
    }

    @Test
    void shouldKeepLiveEntriesDuringTheSweep() {
        ConversationState state = state();
        state.recordQuery(USER_ID, "свежий вопрос", context());
        state.recordOperatorReply(USER_ID);

        state.evictExpired();

        assertTrue(state.lastQuery(USER_ID).isPresent());
        assertTrue(state.isOperatorRecentlyActive(USER_ID));
    }
}
