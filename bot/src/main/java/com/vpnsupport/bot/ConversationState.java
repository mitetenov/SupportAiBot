package com.vpnsupport.bot;

import com.vpnsupport.config.ConversationProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Short-lived per-user conversation state: what the user last asked and when an
 * operator last replied to them.
 *
 * <p>Both facts expire, and both are pruned on a schedule. The maps they replace
 * were plain {@link ConcurrentHashMap}s that nothing ever removed from, so they
 * grew for the lifetime of the process — one entry per user who ever wrote in.
 */
@Component
public class ConversationState {

    private static final Logger log = LoggerFactory.getLogger(ConversationState.class);

    private final Map<Long, LastQuery> lastQueries = new ConcurrentHashMap<>();
    private final Map<Long, Long> lastOperatorReplyAt = new ConcurrentHashMap<>();

    private final Duration operatorSuppressionWindow;
    private final Duration lastQueryTtl;

    public ConversationState(ConversationProperties properties) {
        this.operatorSuppressionWindow = properties.getOperatorSuppressionWindow();
        this.lastQueryTtl = properties.getLastQueryTtl();
    }

    public void recordQuery(long userId, String query, FaqEmbeddingService.FaqContext faqContext) {
        if (query == null || query.isBlank()) {
            return;
        }
        lastQueries.put(userId, new LastQuery(query, faqContext, System.currentTimeMillis()));
    }

    public Optional<LastQuery> lastQuery(long userId) {
        LastQuery last = lastQueries.get(userId);
        if (last == null) {
            return Optional.empty();
        }
        if (isExpired(last.recordedAt(), lastQueryTtl)) {
            lastQueries.remove(userId);
            return Optional.empty();
        }
        return Optional.of(last);
    }

    public void recordOperatorReply(long userId) {
        lastOperatorReplyAt.put(userId, System.currentTimeMillis());
    }

    /**
     * True while the AI should stay out of the way because a human is handling
     * this conversation.
     */
    public boolean isOperatorRecentlyActive(long userId) {
        Long at = lastOperatorReplyAt.get(userId);
        if (at == null) {
            return false;
        }
        if (isExpired(at, operatorSuppressionWindow)) {
            lastOperatorReplyAt.remove(userId);
            return false;
        }
        return true;
    }

    public void clear(long userId) {
        lastQueries.remove(userId);
        lastOperatorReplyAt.remove(userId);
    }

    @Scheduled(fixedDelay = 15, timeUnit = java.util.concurrent.TimeUnit.MINUTES)
    public void evictExpired() {
        int before = lastQueries.size() + lastOperatorReplyAt.size();
        lastQueries.entrySet().removeIf(e -> isExpired(e.getValue().recordedAt(), lastQueryTtl));
        lastOperatorReplyAt.entrySet().removeIf(e -> isExpired(e.getValue(), operatorSuppressionWindow));
        int removed = before - lastQueries.size() - lastOperatorReplyAt.size();
        if (removed > 0) {
            log.debug("Evicted {} stale conversation-state entries", removed);
        }
    }

    private static boolean isExpired(long timestamp, Duration ttl) {
        return System.currentTimeMillis() - timestamp >= ttl.toMillis();
    }

    /** The user's most recent question and the FAQ retrieval it produced. */
    public record LastQuery(String text, FaqEmbeddingService.FaqContext faqContext, long recordedAt) {

        public FaqEmbeddingService.FaqContext faqContextOrEmpty() {
            return faqContext != null ? faqContext : FaqEmbeddingService.FaqContext.EMPTY;
        }
    }
}
