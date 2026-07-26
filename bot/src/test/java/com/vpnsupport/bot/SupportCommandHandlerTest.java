package com.vpnsupport.bot;

import com.vpnsupport.config.TelegramProperties;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.domain.Pageable;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class SupportCommandHandlerTest {

    private static final long ADMIN_ID = 111L;
    private static final long USER_ID = 222L;
    private static final long CHAT_ID = 900L;

    @Mock private TelegramMessageSender messageSender;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;
    @Mock private KnowledgeGapService knowledgeGapService;
    @Mock private UserRepository userRepository;

    private SupportCommandHandler handler;

    /**
     * Echoes "key|arg0|arg1|..." so assertions can check which values were
     * formatted in without pinning the actual Russian wording. A fake rather
     * than a mock: Mockito's varargs matching does not cope with the mix of
     * zero-, one- and five-argument calls this class makes.
     */
    private static final BotMessages ECHO_MESSAGES = new BotMessages(null) {
        @Override
        public String get(String key, Object... args) {
            StringBuilder sb = new StringBuilder(key);
            for (Object arg : args) {
                sb.append('|').append(arg);
            }
            return sb.toString();
        }
    };

    @BeforeEach
    void setUp() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportAdminTelegramIds(String.valueOf(ADMIN_ID));

        handler = new SupportCommandHandler(messageSender, tokenUsageRepository,
                knowledgeGapService, userRepository, ECHO_MESSAGES, properties);
    }

    private String sentText() {
        ArgumentCaptor<String> captor = ArgumentCaptor.forClass(String.class);
        verify(messageSender).send(eq(CHAT_ID), captor.capture());
        return captor.getValue();
    }

    // ------------------------------------------------------------------ gating

    @Test
    void shouldRecogniseSlashCommands() {
        assertTrue(handler.isCommand("/start"));
        assertFalse(handler.isCommand("не работает"));
        assertFalse(handler.isCommand(null));
    }

    @Test
    void shouldNotRunAdminCommandsForOrdinaryUsers() {
        assertFalse(handler.handleAdminCommand(CHAT_ID, USER_ID, "/stats"));
        assertFalse(handler.handleAdminCommand(CHAT_ID, USER_ID, "/gaps"));

        verifyNoInteractions(tokenUsageRepository, knowledgeGapService, messageSender);
    }

    @Test
    void shouldNotClaimUnknownCommandsFromAnAdmin() {
        assertFalse(handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/whatever"));
    }

    // ------------------------------------------------------------------- /stats

    /**
     * The argument is overloaded: small numbers mean "how many rows", large ones
     * mean "which user". {@code /stats 100} is the last value read as a row
     * count, {@code /stats 101} is already a Telegram ID.
     */
    @ParameterizedTest
    @CsvSource({"1", "10", "50", "100"})
    void shouldReadASmallArgumentAsARowCount(int limit) {
        when(tokenUsageRepository.findTopByTokens(any(Pageable.class)))
                .thenReturn(List.of(new TokenStatsDto(USER_ID, 1000L, 600L, 400L, 5L)));

        assertTrue(handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats " + limit));

        ArgumentCaptor<Pageable> pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(tokenUsageRepository).findTopByTokens(pageable.capture());
        assertEquals(limit, pageable.getValue().getPageSize());
        verify(tokenUsageRepository, never()).getStatsByTelegramId(anyLong());
    }

    @Test
    void shouldReadALargeArgumentAsATelegramId() {
        when(tokenUsageRepository.getStatsByTelegramId(123_456_789L))
                .thenReturn(List.of(new TokenStatsDto(123_456_789L, 1500L, 1000L, 500L, 3L)));

        assertTrue(handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 123456789"));

        verify(tokenUsageRepository).getStatsByTelegramId(123_456_789L);
        verify(tokenUsageRepository, never()).findTopByTokens(any());
    }

    @Test
    void shouldClampANonPositiveRowCount() {
        when(tokenUsageRepository.findTopByTokens(any(Pageable.class)))
                .thenReturn(List.of(new TokenStatsDto(USER_ID, 1L, 1L, 0L, 1L)));

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 0");

        ArgumentCaptor<Pageable> pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(tokenUsageRepository).findTopByTokens(pageable.capture());
        // PageRequest rejects a size of zero outright, so the clamp matters.
        assertEquals(1, pageable.getValue().getPageSize());
    }

    @Test
    void shouldFallBackToTheDefaultListingForAGarbageArgument() {
        when(tokenUsageRepository.findTopByTokens(any(Pageable.class)))
                .thenReturn(List.of(new TokenStatsDto(USER_ID, 1L, 1L, 0L, 1L)));

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats абв");

        ArgumentCaptor<Pageable> pageable = ArgumentCaptor.forClass(Pageable.class);
        verify(tokenUsageRepository).findTopByTokens(pageable.capture());
        assertEquals(10, pageable.getValue().getPageSize());
    }

    @Test
    void shouldReportAnEmptyLeaderboard() {
        when(tokenUsageRepository.findTopByTokens(any(Pageable.class))).thenReturn(List.of());

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats");

        assertEquals("bot.stats.empty", sentText());
    }

    @Test
    void shouldReportNoDataForAnUnknownUser() {
        when(tokenUsageRepository.getStatsByTelegramId(999_999L)).thenReturn(List.of());

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 999999");

        assertTrue(sentText().startsWith("bot.stats.no.data"));
    }

    @Test
    void shouldShowAUsernameWhenOneIsKnown() {
        when(tokenUsageRepository.getStatsByTelegramId(999_999L))
                .thenReturn(List.of(new TokenStatsDto(999_999L, 100L, 60L, 40L, 2L)));
        when(userRepository.findById(999_999L))
                .thenReturn(Optional.of(new UserEntity(999_999L, "johndoe", "John", null)));

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 999999");

        assertTrue(sentText().contains("@johndoe (999999)"));
    }

    @Test
    void shouldFallBackToTheRawIdWhenTheUserIsUnknown() {
        when(tokenUsageRepository.getStatsByTelegramId(999_999L))
                .thenReturn(List.of(new TokenStatsDto(999_999L, 100L, 60L, 40L, 2L)));
        when(userRepository.findById(999_999L)).thenReturn(Optional.empty());

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 999999");

        assertTrue(sentText().contains("999999"));
    }

    @Test
    void shouldSurviveARepositoryFailureWhileResolvingANameAndStillAnswer() {
        when(tokenUsageRepository.getStatsByTelegramId(999_999L))
                .thenReturn(List.of(new TokenStatsDto(999_999L, 100L, 60L, 40L, 2L)));
        when(userRepository.findById(999_999L)).thenThrow(new RuntimeException("db down"));

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/stats 999999");

        assertTrue(sentText().contains("999999"));
    }

    // -------------------------------------------------------------------- /gaps

    @Test
    void shouldReportAnEmptyGapList() {
        when(knowledgeGapService.getTopGaps()).thenReturn(List.of());

        assertTrue(handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/gaps"));

        assertEquals("bot.gaps.empty", sentText());
    }

    @Test
    void shouldListGapsWithRankCountAndTrigger() {
        when(knowledgeGapService.getTopGaps()).thenReturn(List.of(
                new GapStatsDto("как вернуть деньги", 7, "ESCALATED", Instant.now(), Instant.now()),
                new GapStatsDto("не приходит смс", 3, "NO_MATCH", Instant.now(), Instant.now())));

        handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/gaps");

        String text = sentText();
        assertTrue(text.contains("bot.gaps.header"));
        assertTrue(text.contains("1|7|как вернуть деньги|ESCALATED"));
        assertTrue(text.contains("2|3|не приходит смс|NO_MATCH"));
    }

    @Test
    void shouldNotMatchGapsWithAnArgument() {
        // Only the bare command is a gaps request; "/gapsfoo" must not run it.
        assertFalse(handler.handleAdminCommand(CHAT_ID, ADMIN_ID, "/gapsfoo"));
        verifyNoInteractions(knowledgeGapService);
    }

    // -------------------------------------------------------------- formatNumber

    @ParameterizedTest
    @CsvSource({
            "0, 0",
            "999, 999",
            "1000, 1.0K",
            "1500, 1.5K",
            "999999, 1000.0K",
            "1000000, 1.0M",
            "2500000, 2.5M",
            "1000000000, 1.0B"
    })
    void shouldFormatTokenCountsCompactly(long value, String expected) {
        assertEquals(expected, SupportCommandHandler.formatNumber(value));
    }
}
