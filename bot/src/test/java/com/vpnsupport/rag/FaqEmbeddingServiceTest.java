package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class FaqEmbeddingServiceTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    @Mock
    private EmbeddingProvider embeddingProvider;

    private ObjectMapper objectMapper;
    private FaqEmbeddingService service;

    @BeforeEach
    void setUp() {
        lenient().when(embeddingProvider.getDimension()).thenReturn(2000);

        objectMapper = new ObjectMapper();
        service = new FaqEmbeddingService(jdbcTemplate, embeddingProvider);
    }

    @Test
    void shouldReturnEmptySearchWhenNotReady() {
        List<FaqEmbeddingService.FaqResult> results = service.search("test");
        assertTrue(results.isEmpty());
    }

    @Test
    void shouldReturnEmptyBuildFaqContextWhenNotReady() {
        FaqEmbeddingService.FaqContext context = service.buildFaqContext("test");
        assertTrue(context.isEmpty());
        assertEquals("", context.text());
    }



    @Test
    void shouldReturnEmptySearchWhenReadyButEmbedFails() {
        service.markReady();

        List<FaqEmbeddingService.FaqResult> results = service.search("test");
        assertTrue(results.isEmpty());
    }

    @Test
    void shouldInitSchema() {
        service.initSchema();

        verify(jdbcTemplate).execute("CREATE EXTENSION IF NOT EXISTS vector");
        verify(jdbcTemplate).execute(contains("CREATE TABLE IF NOT EXISTS faq ("));
        verify(jdbcTemplate, atLeastOnce()).execute(contains("ALTER TABLE faq"));
    }

    @Test
    void shouldClearFaq() {
        service.clearFaq();
        verify(jdbcTemplate).execute("DELETE FROM faq");
    }

    @Test
    void shouldMarkReady() {
        service.markReady();
        assertFalse(service.search("test") instanceof List<?> list && !list.isEmpty());
    }



    @Test
    void shouldHandleNullQueryInBuildFaqContext() {
        FaqEmbeddingService.FaqContext context = service.buildFaqContext(null);
        assertTrue(context.isEmpty());
        assertEquals("", context.text());
        assertEquals(0.0, context.maxSimilarity());
        assertNull(context.bestQuestion());
    }

    @Test
    void shouldHandleBlankQueryInBuildFaqContext() {
        FaqEmbeddingService.FaqContext context = service.buildFaqContext("  ");
        assertTrue(context.isEmpty());
        assertEquals("", context.text());
        assertEquals(0.0, context.maxSimilarity());
        assertNull(context.bestQuestion());
    }

    @Test
    void shouldGetFaqHashWhenExists() {
        when(jdbcTemplate.queryForObject(
                eq("SELECT val FROM faq_metadata WHERE key = 'faq_hash'"), eq(String.class)))
                .thenReturn("abc123def");

        String hash = service.getFaqHash();

        assertEquals("abc123def", hash);
    }

    @Test
    void shouldGetFaqHashWhenNotExists() {
        when(jdbcTemplate.queryForObject(
                eq("SELECT val FROM faq_metadata WHERE key = 'faq_hash'"), eq(String.class)))
                .thenReturn(null);

        String hash = service.getFaqHash();

        assertNull(hash);
    }

    @Test
    void shouldGetFaqHashWhenError() {
        when(jdbcTemplate.queryForObject(
                eq("SELECT val FROM faq_metadata WHERE key = 'faq_hash'"), eq(String.class)))
                .thenThrow(new RuntimeException("DB error"));

        String hash = service.getFaqHash();

        assertNull(hash);
    }

    @Test
    void shouldUpdateFaqHash() {
        service.updateFaqHash("newhash123");

        verify(jdbcTemplate).update(
                eq("INSERT INTO faq_metadata (key, val) VALUES ('faq_hash', ?) " +
                   "ON CONFLICT (key) DO UPDATE SET val = EXCLUDED.val"),
                eq("newhash123"));
    }

    @Test
    void shouldGetFaqCountWhenHasRows() {
        when(jdbcTemplate.queryForObject(eq("SELECT COUNT(*) FROM faq"), eq(Integer.class)))
                .thenReturn(42);

        Integer count = service.getFaqCount();

        assertEquals(42, count);
    }

    @Test
    void shouldGetFaqCountWhenTableNotExists() {
        when(jdbcTemplate.queryForObject(eq("SELECT COUNT(*) FROM faq"), eq(Integer.class)))
                .thenThrow(new RuntimeException("Table not found"));

        Integer count = service.getFaqCount();

        assertEquals(0, count);
    }

    @Test
    void shouldGetFaqCountWhenNullResult() {
        when(jdbcTemplate.queryForObject(eq("SELECT COUNT(*) FROM faq"), eq(Integer.class)))
                .thenReturn(null);

        Integer count = service.getFaqCount();

        assertNull(count);
    }

    @Test
    void shouldInitSchemaCreateMetadataTable() {
        service.initSchema();

        verify(jdbcTemplate).execute(contains("CREATE TABLE IF NOT EXISTS faq_metadata"));
    }

    @Test
    void shouldIdentifyConnectionIssues() throws Exception {
        var method = FaqEmbeddingService.class.getDeclaredMethod("looksLikeConnectionIssue", String.class);
        method.setAccessible(true);

        assertTrue((Boolean) method.invoke(service, "не могу подключиться"));
        assertTrue((Boolean) method.invoke(service, "VPN не работает"));
        assertTrue((Boolean) method.invoke(service, "медленная скорость"));
        assertFalse((Boolean) method.invoke(service, (String) null));
        assertFalse((Boolean) method.invoke(service, "  "));
        assertFalse((Boolean) method.invoke(service, "как оплатить"));
    }

    @Test
    void shouldCallEmbedProviderOnSearch() {
        when(embeddingProvider.embed(anyString())).thenReturn(new float[]{0.1f, 0.2f});
        when(embeddingProvider.getDimension()).thenReturn(2);

        service.markReady();
        service.search("test query");

        verify(embeddingProvider).embed("test query");
    }

    @Test
    void shouldPassSearchResultToJdbcWhenEmbedSucceeds() {
        when(embeddingProvider.embed(anyString())).thenReturn(new float[]{0.5f, 0.3f});
        when(embeddingProvider.getDimension()).thenReturn(2);

        service.markReady();

        var results = service.search("test");
        assertTrue(results.isEmpty());

        verify(embeddingProvider).embed("test");
    }

    @Test
    void shouldIndexFaqWithEmbedding() {
        when(embeddingProvider.embed(contains("test question"))).thenReturn(new float[]{1.0f});
        when(embeddingProvider.getDimension()).thenReturn(1);

        service.clearFaq();
        service.indexFaq("test question", "test answer", null);

        verify(jdbcTemplate).update(eq("INSERT INTO faq (id, question, answer, embedding, keywords) VALUES (?, ?, ?, ?::vector, ?)"),
                anyString(), eq("test question"), eq("test answer"), anyString(), isNull());
        verify(embeddingProvider).embed(contains("test question"));
    }

    @Test
    void shouldSkipIndexFaqWhenEmbedReturnsNull() {
        when(embeddingProvider.embed(anyString())).thenReturn(null);

        service.clearFaq();
        service.indexFaq("query", "answer", null);

        verify(jdbcTemplate, never()).update(eq("INSERT INTO faq (id, question, answer, embedding, keywords) VALUES (?, ?, ?, ?::vector, ?)"),
                anyString(), anyString(), anyString(), anyString(), any());
    }

    @Test
    void shouldSkipIndexFaqWhenEmbedWrongDimension() {
        when(embeddingProvider.embed(anyString())).thenReturn(new float[]{1.0f, 2.0f});
        when(embeddingProvider.getDimension()).thenReturn(3);

        service.clearFaq();
        service.indexFaq("query", "answer", null);

        verify(jdbcTemplate, never()).update(eq("INSERT INTO faq (id, question, answer, embedding, keywords) VALUES (?, ?, ?, ?::vector, ?)"),
                anyString(), anyString(), anyString(), anyString(), any());
    }

    @Test
    void shouldEmbedQueryAsVector() {
        when(embeddingProvider.getDimension()).thenReturn(2);
        when(embeddingProvider.embed(eq("my query"))).thenReturn(new float[]{0.8f, 0.2f});

        String result = service.embedQueryAsVector("my query");

        assertNotNull(result);
        assertTrue(result.startsWith("["));
        assertTrue(result.contains("0.8"));
        verify(embeddingProvider).embed("my query");
    }

    @Test
    void shouldRejectAnEmbeddingOfTheWrongDimension() {
        when(embeddingProvider.getDimension()).thenReturn(2000);
        when(embeddingProvider.embed(eq("my query"))).thenReturn(new float[]{0.8f, 0.2f});

        assertNull(service.embedQueryAsVector("my query"));
    }

    @Test
    void shouldEmbedEachDistinctTextOnlyOnce() {
        when(embeddingProvider.getDimension()).thenReturn(2);
        when(embeddingProvider.embed(eq("repeat"))).thenReturn(new float[]{0.5f, 0.5f});

        service.embedQueryAsVector("repeat");
        service.embedQueryAsVector("repeat");
        service.embedQueryAsVector("repeat");

        // One provider round-trip, not three: the same message used to trigger
        // several embedding calls across the primary search, the constant
        // fallback searches and knowledge-gap accounting.
        verify(embeddingProvider, times(1)).embed("repeat");
    }
}
