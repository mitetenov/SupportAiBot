package com.vpnsupport.bot;

import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Handles the bot's slash commands.
 *
 * <p>Split out of {@code VpnSupportBot} so the update router stays about
 * routing. Admin-only commands are answered with the same "unknown command"
 * text as anything else when the sender is not an admin — staying silent would
 * itself confirm that the command exists.
 */
@Component
public class SupportCommandHandler {

    /** Above this value a {@code /stats} argument is read as a Telegram ID, not a row count. */
    private static final int STATS_ID_THRESHOLD = 100;
    private static final int DEFAULT_STATS_LIMIT = 10;

    private final TelegramMessageSender messageSender;
    private final LlmTokenUsageRepository tokenUsageRepository;
    private final KnowledgeGapService knowledgeGapService;
    private final UserRepository userRepository;
    private final BotMessages messages;
    private final Set<Long> adminTelegramIds;

    public SupportCommandHandler(TelegramMessageSender messageSender,
                                 LlmTokenUsageRepository tokenUsageRepository,
                                 KnowledgeGapService knowledgeGapService,
                                 UserRepository userRepository,
                                 BotMessages messages,
                                 com.vpnsupport.config.TelegramProperties telegramProperties) {
        this.messageSender = messageSender;
        this.tokenUsageRepository = tokenUsageRepository;
        this.knowledgeGapService = knowledgeGapService;
        this.userRepository = userRepository;
        this.messages = messages;
        this.adminTelegramIds = telegramProperties.getSupportAdminTelegramIds();
    }

    public boolean isCommand(String text) {
        return text != null && text.startsWith("/");
    }

    public boolean isAdmin(long telegramId) {
        return adminTelegramIds.contains(telegramId);
    }

    /**
     * Runs an admin command if {@code text} is one.
     *
     * @return true when the command was recognised and handled
     */
    public boolean handleAdminCommand(long chatId, long telegramId, String text) {
        if (!isAdmin(telegramId)) {
            return false;
        }
        if (text.startsWith("/stats")) {
            handleStats(chatId, text);
            return true;
        }
        if (text.equals("/gaps")) {
            handleGaps(chatId);
            return true;
        }
        return false;
    }

    public void sendHelp(long chatId) {
        messageSender.send(chatId, messages.get("bot.help"));
    }

    public void sendUnknownCommand(long chatId) {
        messageSender.send(chatId, messages.get("bot.unknown.command"));
    }

    private void handleStats(long chatId, String command) {
        String[] parts = command.split("\\s+");
        if (parts.length == 2) {
            try {
                long num = Long.parseLong(parts[1]);
                if (num <= STATS_ID_THRESHOLD) {
                    showTopStats(chatId, Math.clamp(num, 1, STATS_ID_THRESHOLD));
                } else {
                    showUserStats(chatId, num);
                }
                return;
            } catch (NumberFormatException ignored) {
                // fall through to the default listing
            }
        }
        showTopStats(chatId, DEFAULT_STATS_LIMIT);
    }

    private void handleGaps(long chatId) {
        List<GapStatsDto> gaps = knowledgeGapService.getTopGaps();
        if (gaps.isEmpty()) {
            messageSender.send(chatId, messages.get("bot.gaps.empty"));
            return;
        }
        StringBuilder sb = new StringBuilder(messages.get("bot.gaps.header"));
        int rank = 1;
        for (GapStatsDto gap : gaps) {
            sb.append("\n").append(messages.get("bot.gaps.row",
                    rank++, gap.gapCount(), gap.userQuery(), gap.triggerReason()));
        }
        messageSender.send(chatId, sb.toString());
    }

    private void showTopStats(long chatId, int limit) {
        List<TokenStatsDto> top = tokenUsageRepository.findTopByTokens(PageRequest.of(0, limit));
        if (top.isEmpty()) {
            messageSender.send(chatId, messages.get("bot.stats.empty"));
            return;
        }
        StringBuilder sb = new StringBuilder(messages.get("bot.stats.top.header", limit));
        int rank = 1;
        for (TokenStatsDto row : top) {
            sb.append("\n").append(messages.get("bot.stats.top.row",
                    rank++, resolveUserName(row.telegramId()),
                    formatNumber(row.totalTokens()), row.requestCount()));
        }
        messageSender.send(chatId, sb.toString());
    }

    private void showUserStats(long chatId, long telegramId) {
        List<TokenStatsDto> stats = tokenUsageRepository.getStatsByTelegramId(telegramId);
        if (stats.isEmpty()) {
            messageSender.send(chatId, messages.get("bot.stats.no.data", resolveUserName(telegramId)));
            return;
        }
        TokenStatsDto row = stats.get(0);
        messageSender.send(chatId, messages.get("bot.stats.user",
                resolveUserName(telegramId),
                row.requestCount(),
                formatNumber(row.promptTokens()),
                formatNumber(row.completionTokens()),
                formatNumber(row.totalTokens())));
    }

    private String resolveUserName(Long telegramId) {
        try {
            return userRepository.findById(telegramId)
                    .filter(u -> u.getUsername() != null && !u.getUsername().isBlank())
                    .map(u -> "@" + u.getUsername() + " (" + telegramId + ")")
                    .orElseGet(() -> String.valueOf(telegramId));
        } catch (Exception e) {
            return String.valueOf(telegramId);
        }
    }

    /**
     * Formats a token count compactly. Pinned to {@link Locale#ROOT}: with the
     * default locale the decimal separator follows the host's settings, so the
     * same number rendered "1.5K" in one deployment and "1,5K" in another.
     */
    static String formatNumber(long n) {
        if (n < 1_000) return String.valueOf(n);
        if (n < 1_000_000) return String.format(Locale.ROOT, "%.1fK", n / 1_000.0);
        if (n < 1_000_000_000) return String.format(Locale.ROOT, "%.1fM", n / 1_000_000.0);
        return String.format(Locale.ROOT, "%.1fB", n / 1_000_000_000.0);
    }
}
