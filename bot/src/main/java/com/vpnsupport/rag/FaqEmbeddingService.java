package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.config.GeminiProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

@Service
public class FaqEmbeddingService {

    private static final Logger log = LoggerFactory.getLogger(FaqEmbeddingService.class);
    private static final int EMBEDDING_DIMENSION = 2000;
    private static final String EMBEDDING_MODEL = "gemini-embedding-001";
    private static final int SEARCH_LIMIT = 3;
    private static final double MIN_SIMILARITY = 0.5;
    private static final String CONNECTION_FAQ_QUERY =
            "Не могу подключиться к VPN / не работает / не заходит";

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;
    private final String geminiApiKey;

    public FaqEmbeddingService(JdbcTemplate jdbcTemplate,
                                ObjectMapper objectMapper, GeminiProperties geminiProperties) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.geminiApiKey = geminiProperties.getApiKey();
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
                .formatted(EMBEDDING_DIMENSION));
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
        log.info("FAQ schema initialized");
    }

    public void indexFaq(String question, String answer, String images) {
        float[] embedding = embed(question);
        if (embedding == null || embedding.length != EMBEDDING_DIMENSION) {
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
        jdbcTemplate.execute("DELETE FROM faq");
    }

    public List<FaqResult> search(String query) {
        if (geminiApiKey == null || geminiApiKey.isBlank()) {
            return List.of();
        }

        float[] queryEmbedding = embed(query);
        if (queryEmbedding == null || queryEmbedding.length != EMBEDDING_DIMENSION) {
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

    public String buildFaqContext(String userQuery) {
        List<FaqResult> results = search(userQuery);
        if (results.isEmpty() && looksLikeConnectionIssue(userQuery)) {
            results = search(CONNECTION_FAQ_QUERY);
        }
        if (results.isEmpty()) {
            return "";
        }

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
                || lower.contains("обрыв");
    }

    public String buildRefinedFaqContext(String originalQuery, List<String> mcpResults) {
        StringBuilder enrichedQuery = new StringBuilder(originalQuery);
        for (String result : mcpResults) {
            if (result != null && !result.isBlank()) {
                String truncated = result.length() > 500 ? result.substring(0, 500) : result;
                enrichedQuery.append(" ").append(truncated);
            }
        }
        return buildFaqContext(enrichedQuery.toString());
    }

    public List<String> getMatchedImages(String userQuery) {
        List<FaqResult> results = search(userQuery);
        if (results.isEmpty() && looksLikeConnectionIssue(userQuery)) {
            results = search(CONNECTION_FAQ_QUERY);
        }
        return results.stream()
                .filter(r -> r.images() != null && !r.images().isEmpty())
                .flatMap(r -> r.images().stream())
                .map(String::trim)
                .filter(s -> !s.isBlank())
                .distinct()
                .toList();
    }

    private float[] embed(String text) {
        try {
            ObjectNode requestBody = objectMapper.createObjectNode();
            ObjectNode content = requestBody.putObject("content");
            ArrayNode parts = content.putArray("parts");
            parts.addObject().put("text", text);
            requestBody.put("outputDimensionality", EMBEDDING_DIMENSION);

            String response = java.net.http.HttpClient.newHttpClient()
                    .send(java.net.http.HttpRequest.newBuilder()
                            .uri(java.net.URI.create(
                                    "https://generativelanguage.googleapis.com/v1beta/models/"
                                            + EMBEDDING_MODEL + ":embedContent?key="
                                            + geminiApiKey))
                            .header("Content-Type", "application/json")
                            .POST(java.net.http.HttpRequest.BodyPublishers.ofString(
                                    objectMapper.writeValueAsString(requestBody)))
                            .build(),
                            java.net.http.HttpResponse.BodyHandlers.ofString())
                    .body();

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode values = jsonResponse.get("embedding").get("values");
            if (values == null || !values.isArray()) {
                log.error("Unexpected embedding response: {}", response);
                return null;
            }

            float[] result = new float[values.size()];
            for (int i = 0; i < values.size(); i++) {
                result[i] = values.get(i).floatValue();
            }
            return result;

        } catch (Exception e) {
            log.error("Embedding request failed", e);
            return null;
        }
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
