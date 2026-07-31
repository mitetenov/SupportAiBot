package com.vpnsupport.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

@Service
public class FaqEmbeddingService {

    private static final Logger log = LoggerFactory.getLogger(FaqEmbeddingService.class);

    private static final int SEARCH_LIMIT = 3;
    private static final int MAX_RESULTS = 5;

    /** Cosine-similarity floor for the vector channel. */
    private static final double MIN_VECTOR_SIMILARITY = 0.65;

    /**
     * {@code ts_rank} floor for the lexical channel.
     *
     * <p>Deliberately near zero. {@code websearch_to_tsquery} ANDs its terms, so
     * a match already means every word of the query is present — the rank that
     * follows reflects term frequency and entry length, not relevance, and is a
     * poor thing to filter on. Measured against the real corpus, correct matches
     * ranged from 0.0398 to 0.5458: the previous 0.05 floor cut off a legitimate
     * hit ("как настроить впн на пк" scores 0.0398 on the setup entry).
     */
    private static final double MIN_FTS_RANK = 0.01;

    /** RRF damping constant; 60 is the value from the original Cormack et al. paper. */
    private static final int RRF_K = 60;

    private static final int EMBEDDING_CACHE_SIZE = 256;

    /**
     * Spellings of the product's own name, appended to every entry so a query
     * written in Cyrillic still matches a corpus written in Latin.
     * See {@link #withGlobalAliases(String)}.
     */
    private static final List<String> GLOBAL_SEARCH_ALIASES = List.of("vpn", "впн", "вэпэн");

    /** Sentinel for "could not embed"; never a valid result. */
    private static final float[] EMPTY_EMBEDDING = new float[0];

    private static final String CONNECTION_FAQ_QUERY =
            "Не могу подключиться к VPN / не работает / не заходит";
    private static final String REFERRAL_FAQ_QUERY =
            "Реферальная программа, партнёрка, реферальная ссылка, пригласить друга, бонусы, рефералы";

    /**
     * Ranks each entry independently in the vector and the lexical channel and
     * fuses the two by Reciprocal Rank Fusion. The previous weighted sum
     * (0.7·cosine + 0.3·ts_rank) compared two incomparable scales: real
     * {@code ts_rank} values sit around 0.05, so the lexical term could never
     * lift an entry past the admission threshold on its own and keyword-only
     * matches were silently unreachable.
     *
     * <p>The lexical rank only contributes when there actually was a match,
     * otherwise every non-matching row would still receive a rank and dilute
     * the fusion.
     */
    private static final String HYBRID_SEARCH_SQL = """
            WITH scored AS (
                SELECT question,
                       answer,
                       1 - (embedding <=> ?::vector) AS vector_sim,
                       ts_rank(to_tsvector('russian',
                                   question || ' ' || COALESCE(keywords, '') || ' ' || answer),
                               websearch_to_tsquery('russian', ?)) AS fts_rank
                FROM faq
                WHERE embedding IS NOT NULL
            ),
            ranked AS (
                SELECT question, answer, vector_sim, fts_rank,
                       RANK() OVER (ORDER BY vector_sim DESC) AS vector_pos,
                       RANK() OVER (ORDER BY fts_rank DESC)   AS fts_pos
                FROM scored
            )
            SELECT question, answer, vector_sim, fts_rank,
                   (1.0 / (? + vector_pos))
                   + CASE WHEN fts_rank > 0 THEN 1.0 / (? + fts_pos) ELSE 0 END AS rrf_score
            FROM ranked
            WHERE vector_sim >= ? OR fts_rank >= ?
            ORDER BY rrf_score DESC
            LIMIT ?
            """;

    private static final String VECTOR_SEARCH_SQL = """
            SELECT question, answer, 1 - (embedding <=> ?::vector) AS vector_sim
            FROM faq
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> ?::vector
            LIMIT ?
            """;

    private final JdbcTemplate jdbcTemplate;
    private final EmbeddingProvider embeddingProvider;
    private volatile boolean ready = false;

    /**
     * Access-ordered LRU over query embeddings. Embeddings are a pure function
     * of the text, so entries never go stale. This collapses the repeated
     * provider round-trips a single user message used to trigger: the primary
     * search, the two constant fallback searches and the knowledge-gap insert
     * all hit the same cache.
     */
    private final Map<String, float[]> embeddingCache = Collections.synchronizedMap(
            new LinkedHashMap<>(16, 0.75f, true) {
                @Override
                protected boolean removeEldestEntry(Map.Entry<String, float[]> eldest) {
                    return size() > EMBEDDING_CACHE_SIZE;
                }
            });

    public FaqEmbeddingService(JdbcTemplate jdbcTemplate, EmbeddingProvider embeddingProvider) {
        this.jdbcTemplate = jdbcTemplate;
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
        jdbcTemplate.execute("ALTER TABLE faq DROP COLUMN IF EXISTS images");
        jdbcTemplate.execute("ALTER TABLE faq ADD COLUMN IF NOT EXISTS keywords VARCHAR(2000)");
        try {
            jdbcTemplate.execute("""
                    CREATE INDEX IF NOT EXISTS faq_embedding_idx
                    ON faq USING hnsw (embedding vector_cosine_ops)
                    """);
            jdbcTemplate.execute("""
                    CREATE INDEX IF NOT EXISTS faq_fts_idx
                    ON faq USING gin (to_tsvector('russian', question || ' ' || COALESCE(keywords, '') || ' ' || answer))
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

    public void indexFaq(String question, String answer, String keywords) {
        String searchable = withGlobalAliases(keywords);
        String embedText = question + " " + searchable + "\n" + answer;
        float[] embedding = embeddingProvider.embed(embedText);
        if (embedding == null || embedding.length != embeddingProvider.getDimension()) {
            log.warn("Failed to embed FAQ: {}", question);
            return;
        }

        jdbcTemplate.update(
                "INSERT INTO faq (id, question, answer, embedding, keywords) VALUES (?, ?, ?, ?::vector, ?)",
                UUID.randomUUID().toString(), question, answer, vectorToString(embedding), searchable);
        log.debug("Indexed FAQ: {} keywords={}", question, searchable);
    }

    /**
     * Adds the spellings of the product's own name to every entry.
     *
     * <p>The FAQ writes "VPN" in Latin; Russian users routinely type "впн".
     * {@code websearch_to_tsquery} ANDs its terms, so a single unmatched word
     * sinks the whole query: measured against the real corpus, "как настроить
     * впн на пк" matched zero entries while "как настроить vpn на пк" matched
     * the right one.
     *
     * <p>Making the term match everywhere is the point. In a VPN support base
     * every entry is about VPN, so the word carries no information and should
     * not decide anything — the remaining words do the discriminating.
     */
    private static String withGlobalAliases(String keywords) {
        String base = keywords != null && !keywords.isBlank() ? keywords + ", " : "";
        return base + String.join(", ", GLOBAL_SEARCH_ALIASES);
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
        if (!ready || query == null || query.isBlank()) {
            return List.of();
        }

        String vectorStr = embedQueryAsVector(query);
        if (vectorStr == null) {
            return List.of();
        }

        String rawClean = query.replaceAll("[^a-zA-Zа-яА-Я0-9\\s]", " ").trim();
        String cleanQuery = rawClean.isBlank() ? query : rawClean;

        List<FaqResult> results = new ArrayList<>();
        try {
            jdbcTemplate.query(
                    HYBRID_SEARCH_SQL,
                    ps -> {
                        ps.setString(1, vectorStr);
                        ps.setString(2, cleanQuery);
                        ps.setInt(3, RRF_K);
                        ps.setInt(4, RRF_K);
                        ps.setDouble(5, MIN_VECTOR_SIMILARITY);
                        ps.setDouble(6, MIN_FTS_RANK);
                        ps.setInt(7, SEARCH_LIMIT);
                    },
                    // Block body, not an expression: an expression lambda here is
                    // ambiguous between ResultSetExtractor and RowCallbackHandler.
                    rs -> {
                        results.add(new FaqResult(
                                rs.getString("question"),
                                rs.getString("answer"),
                                rs.getDouble("vector_sim"),
                                rs.getDouble("rrf_score")));
                    }
            );
        } catch (Exception e) {
            log.warn("FAQ hybrid search failed, falling back to vector search: {}", e.getMessage());
            return searchPureVector(vectorStr);
        }

        return results;
    }

    private List<FaqResult> searchPureVector(String vectorStr) {
        List<FaqResult> results = new ArrayList<>();
        try {
            jdbcTemplate.query(
                    VECTOR_SEARCH_SQL,
                    ps -> {
                        ps.setString(1, vectorStr);
                        ps.setString(2, vectorStr);
                        ps.setInt(3, SEARCH_LIMIT);
                    },
                    rs -> {
                        double similarity = rs.getDouble("vector_sim");
                        if (similarity >= MIN_VECTOR_SIMILARITY) {
                            results.add(new FaqResult(
                                    rs.getString("question"),
                                    rs.getString("answer"),
                                    similarity,
                                    similarity));
                        }
                    }
            );
        } catch (Exception e) {
            log.warn("FAQ pure vector search failed: {}", e.getMessage());
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

        results.sort((a, b) -> Double.compare(b.rrfScore(), a.rrfScore()));
        if (results.size() > MAX_RESULTS) {
            results = new ArrayList<>(results.subList(0, MAX_RESULTS));
        }
        return results;
    }

    private void mergeDeduped(List<FaqResult> target, List<FaqResult> source) {
        Set<String> existing = new LinkedHashSet<>();
        for (FaqResult r : target) {
            existing.add(r.question());
        }
        for (FaqResult r : source) {
            if (existing.add(r.question())) {
                target.add(r);
            }
        }
    }

    public FaqContext buildFaqContext(String userQuery) {
        return buildFaqContext(userQuery, Set.of());
    }

    /**
     * Retrieves the FAQ entries to put in front of the model and returns them
     * together with the metadata the caller needs. Everything a caller might
     * want about this retrieval is in the returned value — the previous version
     * stashed the similarity, the best question and the shown set in
     * {@link ThreadLocal}s that were never cleared, which only worked as long as
     * every step of a request stayed on the same pooled thread.
     */
    public FaqContext buildFaqContext(String userQuery, Set<String> excludeQuestions) {
        List<FaqResult> results = searchWithFallback(userQuery);
        if (excludeQuestions != null && !excludeQuestions.isEmpty()) {
            results = results.stream()
                    .filter(r -> !excludeQuestions.contains(r.question()))
                    .toList();
        }
        if (results.isEmpty()) {
            return FaqContext.EMPTY;
        }

        StringBuilder sb = new StringBuilder(
                "FAQ (скопируй инструкцию дословно в ответ, не добавляй своих шагов):\n");
        for (FaqResult r : results) {
            sb.append("Вопрос: ").append(r.question()).append("\n");
            sb.append("Инструкция: ").append(r.answer()).append("\n\n");
        }

        double maxSimilarity = results.stream()
                .mapToDouble(FaqResult::similarity)
                .max()
                .orElse(0.0);

        return new FaqContext(sb.toString(), List.copyOf(results),
                maxSimilarity, results.get(0).question());
    }

    /** Embeds {@code text} and renders it as a pgvector literal, or null on failure. */
    public String embedQueryAsVector(String text) {
        float[] embedding = embed(text);
        return embedding.length > 0 ? vectorToString(embedding) : null;
    }

    /** @return the embedding, or an empty array when it could not be produced */
    private float[] embed(String text) {
        if (text == null || text.isBlank()) {
            return EMPTY_EMBEDDING;
        }
        float[] cached = embeddingCache.get(text);
        if (cached != null) {
            return cached;
        }
        float[] embedding = embeddingProvider.embed(text);
        if (embedding == null || embedding.length != embeddingProvider.getDimension()) {
            return EMPTY_EMBEDDING;
        }
        embeddingCache.put(text, embedding);
        return embedding;
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
                || lower.contains("друг")
                || lower.contains("друз")
                || lower.contains("бонус");
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

    /**
     * A single retrieved FAQ entry.
     *
     * @param similarity raw cosine similarity — the knowledge-gap thresholds are
     *                   expressed on this scale, so it stays separate from the
     *                   fused ranking score
     * @param rrfScore   Reciprocal Rank Fusion score used for ordering only
     */
    public record FaqResult(String question, String answer, double similarity, double rrfScore) {
    }

    /** Everything one FAQ retrieval produced. */
    public record FaqContext(String text, List<FaqResult> results,
                             double maxSimilarity, String bestQuestion) {

        public static final FaqContext EMPTY = new FaqContext("", List.of(), 0.0, null);

        public boolean isEmpty() {
            return results.isEmpty();
        }

        /** Questions shown to the model, in rank order. */
        public Set<String> questions() {
            return results.stream()
                    .map(FaqResult::question)
                    .collect(java.util.stream.Collectors.toCollection(LinkedHashSet::new));
        }
    }
}
