package com.vpnsupport.bot;

import com.vpnsupport.rag.FaqEmbeddingService;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

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

    private final JdbcTemplate jdbcTemplate;
    private final FaqEmbeddingService faqEmbeddingService;

    public KnowledgeGapService(JdbcTemplate jdbcTemplate, FaqEmbeddingService faqEmbeddingService) {
        this.jdbcTemplate = jdbcTemplate;
        this.faqEmbeddingService = faqEmbeddingService;
    }

    @PostConstruct
    public void init() {
        initSchema();
    }

    public void initSchema() {
        log.info("Initializing knowledge_gaps schema");
        jdbcTemplate.execute("CREATE EXTENSION IF NOT EXISTS vector");
        jdbcTemplate.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    id BIGSERIAL PRIMARY KEY,
                    user_query VARCHAR(2000) NOT NULL,
                    embedding VECTOR(2000),
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
                """);
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

    public void evaluate(String userQuery, long telegramUserId, String rawBotResponse) {
        try {
            if (userQuery == null || userQuery.isBlank()) {
                return;
            }
            String query = truncate(userQuery, MAX_QUERY_LENGTH);
            String response = truncate(rawBotResponse, MAX_RESPONSE_LENGTH);

            double maxSimilarity = faqEmbeddingService.getLastMaxSimilarity();
            String bestFaqQuestion = faqEmbeddingService.getLastFaqQuestion();
            int faqCount = maxSimilarity > 0 ? 1 : 0;

            String trigger = determineTrigger(rawBotResponse, maxSimilarity, bestFaqQuestion);
            if (trigger == null) {
                return;
            }

            storeGap(query, telegramUserId, bestFaqQuestion, maxSimilarity, faqCount, trigger, response);
        } catch (Exception e) {
            log.warn("Failed to evaluate knowledge gap: {}", e.getMessage());
        }
    }

    public void evaluateOperatorRequest(String userQuery, long telegramUserId) {
        if (userQuery == null || userQuery.isBlank()) {
            return;
        }

        String query = truncate(userQuery, MAX_QUERY_LENGTH);
        String botResponse = "[Пользователь запросил оператора после ответа бота]";

        double maxSimilarity = faqEmbeddingService.getLastMaxSimilarity();
        String bestFaqQuestion = faqEmbeddingService.getLastFaqQuestion();
        int faqCount = maxSimilarity > 0 ? 1 : 0;

        try {
            storeGap(query, telegramUserId, bestFaqQuestion, maxSimilarity, faqCount,
                    "USER_OPERATOR", botResponse);
        } catch (Exception e) {
            log.warn("Failed to evaluate operator knowledge gap: {}", e.getMessage());
        }
    }

    public List<GapStatsDto> getTopGaps(int limit) {
        int safeLimit = Math.clamp(limit, 1, 100);
        List<GapStatsDto> gaps = new ArrayList<>();
        try {
            jdbcTemplate.query(
                    "SELECT user_query, gap_count, trigger_reason, first_seen, last_seen " +
                    "FROM knowledge_gaps ORDER BY gap_count DESC LIMIT ?",
                    ps -> ps.setInt(1, safeLimit),
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

    private void storeGap(String userQuery, long telegramUserId, String bestFaqQuestion,
                          double maxSimilarity, int faqCount, String triggerReason, String botResponse) {
        String vectorStr = faqEmbeddingService.embedQueryAsVector(userQuery);
        if (vectorStr == null) {
            insertGap(userQuery, null, telegramUserId, bestFaqQuestion, maxSimilarity,
                    faqCount, triggerReason, botResponse);
            return;
        }

        Long existingId = findSimilarGap(vectorStr);
        if (existingId != null) {
            jdbcTemplate.update(
                    "UPDATE knowledge_gaps SET gap_count = gap_count + 1, last_seen = ? WHERE id = ?",
                    Instant.now(), existingId);
            log.debug("Incremented gap count for existing gap id={}", existingId);
        } else {
            insertGap(userQuery, vectorStr, telegramUserId, bestFaqQuestion, maxSimilarity,
                    faqCount, triggerReason, botResponse);
        }
    }

    private void insertGap(String userQuery, String vectorStr, long telegramUserId,
                           String bestFaqQuestion, double maxSimilarity, int faqCount,
                           String triggerReason, String botResponse) {
        Instant now = Instant.now();
        jdbcTemplate.update(
                "INSERT INTO knowledge_gaps (user_query, embedding, telegram_id, best_faq_question, " +
                "max_similarity, faq_count, trigger_reason, bot_response, first_seen, last_seen) " +
                "VALUES (?, ?::vector, ?, ?, ?, ?, ?, ?, ?, ?)",
                userQuery,
                vectorStr,
                telegramUserId,
                bestFaqQuestion,
                maxSimilarity,
                faqCount,
                triggerReason,
                botResponse,
                now,
                now);
        log.debug("Inserted new knowledge gap: trigger={}, query='{}'", triggerReason, userQuery);
    }

    private Long findSimilarGap(String vectorStr) {
        try {
            List<Long> ids = new ArrayList<>();
            jdbcTemplate.query(
                    "SELECT id, 1 - (embedding <=> ?::vector) AS similarity FROM knowledge_gaps " +
                    "WHERE embedding IS NOT NULL ORDER BY embedding <=> ?::vector LIMIT 1",
                    ps -> {
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
