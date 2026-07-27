package com.vpnsupport.bot;

import com.vpnsupport.rag.EmbeddingProvider;
import com.vpnsupport.rag.FaqEmbeddingService;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.PreparedStatementSetter;
import org.springframework.stereotype.Service;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

@Service
public class KnowledgeGapService {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeGapService.class);
    private static final double DEDUP_SIMILARITY_THRESHOLD = 0.85;
    private static final int DEFAULT_TOP_LIMIT = 15;
    private static final int MAX_QUERY_LENGTH = 2000;
    private static final int MAX_RESPONSE_LENGTH = 500;

    private static final String INSERT_GAP_SQL =
            "INSERT INTO knowledge_gaps (user_query, embedding, telegram_id, best_faq_question, "
            + "max_similarity, faq_count, trigger_reason, bot_response, first_seen, last_seen) "
            + "VALUES (?, CAST(? AS vector), ?, ?, ?, ?, ?, ?, ?, ?)";

    private final JdbcTemplate jdbcTemplate;
    private final FaqEmbeddingService faqEmbeddingService;
    private final EmbeddingProvider embeddingProvider;

    public KnowledgeGapService(JdbcTemplate jdbcTemplate, FaqEmbeddingService faqEmbeddingService,
                                EmbeddingProvider embeddingProvider) {
        this.jdbcTemplate = jdbcTemplate;
        this.faqEmbeddingService = faqEmbeddingService;
        this.embeddingProvider = embeddingProvider;
    }

    @PostConstruct
    public void init() {
        initSchema();
    }

    public void initSchema() {
        log.info("Initializing knowledge_gaps schema");
        jdbcTemplate.execute("CREATE EXTENSION IF NOT EXISTS vector");
        int dim = embeddingProvider.getDimension();
        jdbcTemplate.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    id BIGSERIAL PRIMARY KEY,
                    user_query VARCHAR(2000) NOT NULL,
                    embedding VECTOR(%d),
                    best_faq_question VARCHAR(2000),
                    max_similarity DOUBLE PRECISION,
                    faq_count INTEGER DEFAULT 0,
                    trigger_reason VARCHAR(20),
                    bot_response VARCHAR(500),
                    gap_count INTEGER DEFAULT 1,
                    first_seen TIMESTAMP NOT NULL,
                    last_seen TIMESTAMP NOT NULL,
                    telegram_id BIGINT
                )
                """.formatted(dim));
        jdbcTemplate.execute("ALTER TABLE knowledge_gaps DROP COLUMN IF EXISTS embedding");
        jdbcTemplate.execute("ALTER TABLE knowledge_gaps ADD COLUMN embedding vector(%d)"
                .formatted(dim));
        try {
            jdbcTemplate.execute("""
                    CREATE INDEX IF NOT EXISTS knowledge_gaps_embedding_idx
                    ON knowledge_gaps USING hnsw (embedding vector_cosine_ops)
                    """);
        } catch (Exception e) {
            log.warn("Could not create knowledge_gaps index: {}", e.getMessage());
        }
        log.info("Knowledge gaps schema initialized");
    }

    /**
     * Records a knowledge gap for {@code userQuery} if the retrieval behind
     * {@code faqContext} looks like it failed the user.
     */
    public void evaluate(String userQuery, long telegramUserId, String rawBotResponse,
                         FaqEmbeddingService.FaqContext faqContext) {
        try {
            if (userQuery == null || userQuery.isBlank()) {
                return;
            }
            String query = truncate(userQuery, MAX_QUERY_LENGTH);
            String response = truncate(rawBotResponse, MAX_RESPONSE_LENGTH);

            FaqEmbeddingService.FaqContext context = orEmpty(faqContext);
            String trigger = determineTrigger(rawBotResponse, context.maxSimilarity(), context.bestQuestion());
            if (trigger == null) {
                return;
            }

            storeGap(new Gap(query, telegramUserId, context.bestQuestion(),
                    context.maxSimilarity(), context.results().size(), trigger, response));
        } catch (Exception e) {
            log.warn("Failed to evaluate knowledge gap: {}", e.getMessage());
        }
    }

    /**
     * Records a gap for a user who asked for a human right after the bot
     * answered — the strongest available signal that the answer missed.
     */
    public void evaluateOperatorRequest(String userQuery, long telegramUserId,
                                        FaqEmbeddingService.FaqContext faqContext) {
        if (userQuery == null || userQuery.isBlank()) {
            return;
        }

        String query = truncate(userQuery, MAX_QUERY_LENGTH);
        String botResponse = "[Пользователь запросил оператора после ответа бота]";
        FaqEmbeddingService.FaqContext context = orEmpty(faqContext);

        try {
            storeGap(new Gap(query, telegramUserId, context.bestQuestion(),
                    context.maxSimilarity(), context.results().size(), "USER_OPERATOR", botResponse));
        } catch (Exception e) {
            log.warn("Failed to evaluate operator knowledge gap: {}", e.getMessage());
        }
    }

    private static FaqEmbeddingService.FaqContext orEmpty(FaqEmbeddingService.FaqContext context) {
        return context != null ? context : FaqEmbeddingService.FaqContext.EMPTY;
    }

    public List<GapStatsDto> getTopGaps(int limit) {
        int safeLimit = Math.clamp(limit, 1, 100);
        List<GapStatsDto> gaps = new ArrayList<>();
        try {
            jdbcTemplate.query(
                    "SELECT user_query, gap_count, trigger_reason, first_seen, last_seen " +
                    "FROM knowledge_gaps ORDER BY gap_count DESC LIMIT ?",
                    ps -> ps.setInt(1, safeLimit),
                    // Block body, not an expression: an expression lambda here is
                    // ambiguous between ResultSetExtractor and RowCallbackHandler.
                    rs -> {
                        gaps.add(new GapStatsDto(
                                rs.getString("user_query"),
                                rs.getInt("gap_count"),
                                rs.getString("trigger_reason"),
                                rs.getTimestamp("first_seen").toInstant(),
                                rs.getTimestamp("last_seen").toInstant()
                        ));
                    }
            );
        } catch (Exception e) {
            log.warn("Failed to get top gaps: {}", e.getMessage());
        }
        return gaps;
    }

    public List<GapStatsDto> getTopGaps() {
        return getTopGaps(DEFAULT_TOP_LIMIT);
    }

    private void storeGap(Gap gap) {
        String vectorStr = faqEmbeddingService.embedQueryAsVector(gap.userQuery());
        if (vectorStr == null) {
            insertGap(gap, null);
            return;
        }

        Long existingId = findSimilarGap(vectorStr);
        if (existingId != null) {
            jdbcTemplate.update(
                    "UPDATE knowledge_gaps SET gap_count = gap_count + 1, last_seen = ? WHERE id = ?",
                    Timestamp.from(Instant.now()), existingId);
            log.debug("Incremented gap count for existing gap id={}", existingId);
        } else {
            insertGap(gap, vectorStr);
        }
    }

    /**
     * Writes the row. {@code CAST(? AS vector)} accepts a null, so a gap whose
     * query could not be embedded takes the same statement as one that could —
     * the two near-identical INSERTs this replaced differed only in that cast.
     */
    private void insertGap(Gap gap, String vectorStr) {
        Timestamp now = Timestamp.from(Instant.now());
        jdbcTemplate.update(INSERT_GAP_SQL,
                gap.userQuery(),
                vectorStr != null && !vectorStr.isBlank() ? vectorStr : null,
                gap.telegramUserId(),
                gap.bestFaqQuestion(),
                gap.maxSimilarity(),
                gap.faqCount(),
                gap.triggerReason(),
                gap.botResponse(),
                now,
                now);
        log.debug("Inserted new knowledge gap: trigger={}, query='{}'",
                gap.triggerReason(), gap.userQuery());
    }

    /** One knowledge-gap row before it is written. */
    private record Gap(String userQuery, long telegramUserId, String bestFaqQuestion,
                       double maxSimilarity, int faqCount, String triggerReason,
                       String botResponse) {
    }

    private Long findSimilarGap(String vectorStr) {
        try {
            List<Long> ids = new ArrayList<>();
            jdbcTemplate.query(
                    "SELECT id, 1 - (embedding <=> CAST(? AS vector)) AS similarity FROM knowledge_gaps " +
                    "WHERE embedding IS NOT NULL ORDER BY embedding <=> CAST(? AS vector) LIMIT 1",
                    (PreparedStatementSetter) ps -> {
                        ps.setString(1, vectorStr);
                        ps.setString(2, vectorStr);
                    },
                    rs -> {
                        double similarity = rs.getDouble("similarity");
                        if (similarity >= DEDUP_SIMILARITY_THRESHOLD) {
                            ids.add(rs.getLong("id"));
                        }
                    }
            );
            return ids.isEmpty() ? null : ids.get(0);
        } catch (Exception e) {
            log.warn("Failed to search similar gaps: {}", e.getMessage());
            return null;
        }
    }

    private String determineTrigger(String rawBotResponse, double maxSimilarity, String bestFaqQuestion) {
        if (bestFaqQuestion == null && maxSimilarity == 0.0) {
            return "NO_MATCH";
        }

        if (maxSimilarity > 0 && maxSimilarity < 0.72) {
            return "LOW_SIMILARITY";
        }

        if (rawBotResponse != null && rawBotResponse.contains("[ESCALATE]")) {
            return "ESCALATED";
        }

        if (isLlmUnsure(rawBotResponse)) {
            return "LLM_UNSURE";
        }

        return null;
    }

    private boolean isLlmUnsure(String response) {
        if (response == null) {
            return false;
        }
        String lower = response.toLowerCase();
        return lower.contains("не знаю")
                || lower.contains("не могу ответить")
                || lower.contains("не могу помочь")
                || lower.contains("затрудняюсь ответить")
                || lower.contains("не обладаю информацией");
    }

    private static String truncate(String s, int maxLength) {
        if (s == null) return null;
        return s.length() > maxLength ? s.substring(0, maxLength) : s;
    }
}
