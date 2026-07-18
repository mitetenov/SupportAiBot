package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

@Service
public class FaqEmbeddingService {

    private static final Logger log = LoggerFactory.getLogger(FaqEmbeddingService.class);
    private static final int SEARCH_LIMIT = 3;
    private static final int MAX_RESULTS = 5;
    private static final double MIN_SIMILARITY = 0.65;
    private static final String CONNECTION_FAQ_QUERY =
            "Не могу подключиться к VPN / не работает / не заходит";
    private static final String REFERRAL_FAQ_QUERY =
            "Реферальная программа, партнёрка, реферальная ссылка, пригласить друга, бонусы, рефералы";

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;
    private final EmbeddingProvider embeddingProvider;
    private volatile boolean ready = false;
    private final ThreadLocal<Double> lastMaxSimilarity = ThreadLocal.withInitial(() -> 0.0);
    private final ThreadLocal<String> lastBestQuestion = new ThreadLocal<>();
    private final ThreadLocal<Set<String>> shownQuestions = new ThreadLocal<>();

    public FaqEmbeddingService(JdbcTemplate jdbcTemplate,
                                ObjectMapper objectMapper, EmbeddingProvider embeddingProvider) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.embeddingProvider = embeddingProvider;
    }

    public void initSchema() {
        log.info("Initializing FAQ schema");
        jdbcTemplate.execute("CREATE EXTENSION IF NOT EXISTS vector");
        jdbcTemplate.execute("""
                CREATE TABLE IF NOT EXISTS faq (
                    id VARCHAR(36) PRIMARY KEY,
                    question VARCHAR(2000) NOT NULL,
                    answer VARCHAR(4000) NOT NULL
                )
                """);
        jdbcTemplate.execute("ALTER TABLE faq DROP COLUMN IF EXISTS embedding");
        jdbcTemplate.execute("ALTER TABLE faq ADD COLUMN embedding vector(%d)"
                .formatted(embeddingProvider.getDimension()));
        jdbcTemplate.execute("ALTER TABLE faq DROP COLUMN IF EXISTS image");
        jdbcTemplate.execute("ALTER TABLE faq ADD COLUMN IF NOT EXISTS images VARCHAR(1000)");
        try {
            jdbcTemplate.execute("""
                    CREATE INDEX IF NOT EXISTS faq_embedding_idx
                    ON faq USING hnsw (embedding vector_cosine_ops)
                    """);
        } catch (Exception e) {
            log.warn("Could not create FAQ index: {}", e.getMessage());
        }

        jdbcTemplate.execute("""
                CREATE TABLE IF NOT EXISTS faq_metadata (
                    key VARCHAR(50) PRIMARY KEY,
                    val VARCHAR(256) NOT NULL
                )
                """);

        log.info("FAQ schema initialized");
    }

    public String getFaqHash() {
        try {
            return jdbcTemplate.queryForObject(
                    "SELECT val FROM faq_metadata WHERE key = 'faq_hash'", String.class);
        } catch (Exception e) {
            return null;
        }
    }

    public void updateFaqHash(String hash) {
        jdbcTemplate.update(
                "INSERT INTO faq_metadata (key, val) VALUES ('faq_hash', ?) " +
                "ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val", hash);
    }

    public Integer getFaqCount() {
        try {
            return jdbcTemplate.queryForObject("SELECT COUNT(*) FROM faq", Integer.class);
        } catch (Exception e) {
            return 0;
        }
    }

    public void indexFaq(String question, String answer, String images) {
        float[] embedding = embeddingProvider.embed(question);
        if (embedding == null || embedding.length != embeddingProvider.getDimension()) {
            log.warn("Failed to embed FAQ: {}", question);
            return;
        }

        String id = UUID.randomUUID().toString();
        String vectorStr = vectorToString(embedding);
        jdbcTemplate.update(
                "INSERT INTO faq (id, question, answer, embedding, images) VALUES (?, ?, ?, ?::vector, ?)",
                id, question, answer, vectorStr, images);
        log.debug("Indexed FAQ: {} images={}", question, images);
    }

    public void clearFaq() {
        ready = false;
        jdbcTemplate.execute("DELETE FROM faq");
    }

    public void markReady() {
        ready = true;
        log.info("FAQ ready for search");
    }

    public List<FaqResult> search(String query) {
        if (!ready) {
            return List.of();
        }

        float[] queryEmbedding = embeddingProvider.embed(query);
        if (queryEmbedding == null || queryEmbedding.length != embeddingProvider.getDimension()) {
            return List.of();
        }

        String vectorStr = vectorToString(queryEmbedding);

        List<FaqResult> results = new ArrayList<>();
        try {
            jdbcTemplate.query(
                    "SELECT question, answer, images, 1 - (embedding <=> ?::vector) AS similarity FROM faq WHERE embedding IS NOT NULL ORDER BY embedding <=> ?::vector LIMIT ?",
                    ps -> {
                        ps.setString(1, vectorStr);
                        ps.setString(2, vectorStr);
                        ps.setInt(3, SEARCH_LIMIT);
                    },
                    rs -> {
                        double similarity = rs.getDouble("similarity");
                        if (similarity >= MIN_SIMILARITY) {
                            results.add(new FaqResult(
                                    rs.getString("question"),
                                    rs.getString("answer"),
                                    splitImages(rs.getString("images")),
                                    similarity
                            ));
                        }
                    }
            );
        } catch (Exception e) {
            log.warn("FAQ search failed: {}", e.getMessage());
        }

        return results;
    }

    private List<FaqResult> searchWithFallback(String query) {
        List<FaqResult> results = new ArrayList<>(search(query));

        if (looksLikeConnectionIssue(query)) {
            mergeDeduped(results, search(CONNECTION_FAQ_QUERY));
        }
        if (looksLikeReferralQuery(query)) {
            mergeDeduped(results, search(REFERRAL_FAQ_QUERY));
        }

        results.sort((a, b) -> Double.compare(b.similarity(), a.similarity()));
        if (results.size() > MAX_RESULTS) {
            results = new ArrayList<>(results.subList(0, MAX_RESULTS));
        }
        return results;
    }

    private void mergeDeduped(List<FaqResult> target, List<FaqResult> source) {
        Set<String> existing = new HashSet<>();
        for (FaqResult r : target) {
            existing.add(r.question());
        }
        for (FaqResult r : source) {
            if (!existing.contains(r.question())) {
                target.add(r);
                existing.add(r.question());
            }
        }
    }

    public String buildFaqContext(String userQuery) {
        return buildFaqContext(userQuery, Set.of());
    }

    public String buildFaqContext(String userQuery, Set<String> excludeQuestions) {
        List<FaqResult> results = searchWithFallback(userQuery);
        if (!excludeQuestions.isEmpty()) {
            results = results.stream()
                    .filter(r -> !excludeQuestions.contains(r.question()))
                    .toList();
        }
        if (results.isEmpty()) {
            lastMaxSimilarity.set(0.0);
            lastBestQuestion.remove();
            shownQuestions.set(Set.of());
            return "";
        }

        shownQuestions.set(results.stream()
                .map(FaqResult::question)
                .collect(java.util.stream.Collectors.toCollection(LinkedHashSet::new)));

        double maxSim = results.stream().mapToDouble(FaqResult::similarity).max().orElse(0.0);
        lastMaxSimilarity.set(maxSim);
        lastBestQuestion.set(results.get(0).question());

        StringBuilder sb = new StringBuilder(
                "FAQ (скопируй инструкцию дословно в ответ, не добавляй своих шагов):\n");
        for (FaqResult r : results) {
            sb.append("Вопрос: ").append(r.question()).append("\n");
            sb.append("Инструкция: ").append(r.answer()).append("\n");
            if (r.images() != null && !r.images().isEmpty()) {
                sb.append("(к ответу прилагаются картинки)\n");
            }
            sb.append("\n");
        }
        return sb.toString();
    }

    public double getLastMaxSimilarity() {
        return lastMaxSimilarity.get();
    }

    public String getLastFaqQuestion() {
        return lastBestQuestion.get();
    }

    public Set<String> getShownQuestions() {
        Set<String> questions = shownQuestions.get();
        return questions != null ? questions : Set.of();
    }

    public boolean looksLikeRejection(String message) {
        if (message == null || message.isBlank()) {
            return false;
        }
        String lower = message.toLowerCase();
        return lower.contains("не то")
                || lower.contains("не подходит")
                || lower.contains("не это")
                || lower.contains("другой вариант")
                || lower.contains("другая инструкция")
                || lower.contains("не та")
                || lower.contains("нет,")
                || lower.contains("другое");
    }

    public String embedQueryAsVector(String text) {
        float[] embedding = embeddingProvider.embed(text);
        return embedding != null ? vectorToString(embedding) : null;
    }

    private boolean looksLikeConnectionIssue(String query) {
        if (query == null || query.isBlank()) {
            return false;
        }
        String lower = query.toLowerCase();
        return lower.contains("подключ")
                || lower.contains("не работ")
                || lower.contains("не заход")
                || lower.contains("vpn")
                || lower.contains("скорост")
                || lower.contains("медлен")
                || lower.contains("сайт")
                || lower.contains("instagram")
                || lower.contains("ошибк")
                || lower.contains("отвали")
                || lower.contains("обрыв")
                || lower.contains("обнов")
                || lower.contains("подписк")
                || lower.contains("пинг")
                || lower.contains("сервер");
    }

    private boolean looksLikeReferralQuery(String query) {
        if (query == null || query.isBlank()) {
            return false;
        }
        String lower = query.toLowerCase();
        return lower.contains("реферал")
                || lower.contains("партнёр")
                || lower.contains("partner")
                || lower.contains("приглас")
                || lower.contains("приглаш")
                || (lower.contains("друг") || lower.contains("друз"))
                || lower.contains("бонус");
    }



    public List<String> getMatchedImages(String userQuery) {
        return searchWithFallback(userQuery).stream()
                .filter(r -> r.images() != null && !r.images().isEmpty())
                .flatMap(r -> r.images().stream())
                .map(String::trim)
                .filter(s -> !s.isBlank())
                .distinct()
                .toList();
    }

    private String vectorToString(float[] vector) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < vector.length; i++) {
            if (i > 0) sb.append(",");
            sb.append(vector[i]);
        }
        sb.append("]");
        return sb.toString();
    }

    public record FaqResult(String question, String answer, List<String> images, double similarity) {
    }

    private static List<String> splitImages(String images) {
        if (images == null || images.isBlank()) {
            return List.of();
        }
        return List.of(images.split(","));
    }
}
