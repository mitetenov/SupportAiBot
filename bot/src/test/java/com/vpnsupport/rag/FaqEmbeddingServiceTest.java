package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.GeminiProperties;
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

    private ObjectMapper objectMapper;
    private FaqEmbeddingService service;

    @BeforeEach
    void setUp() {
        GeminiProperties geminiProperties = new GeminiProperties();
        geminiProperties.setBaseUrl("http://localhost:9999");
        geminiProperties.setApiKey("test-key");

        objectMapper = new ObjectMapper();
        service = new FaqEmbeddingService(jdbcTemplate, objectMapper, geminiProperties);
    }

    @Test
    void shouldReturnEmptySearchWhenNotReady() {
        List<FaqEmbeddingService.FaqResult> results = service.search("test");
        assertTrue(results.isEmpty());
    }

    @Test
    void shouldReturnEmptyBuildFaqContextWhenNotReady() {
        String context = service.buildFaqContext("test");
        assertEquals("", context);
    }

    @Test
    void shouldReturnEmptyMatchedImagesWhenNotReady() {
        List<String> images = service.getMatchedImages("test");
        assertTrue(images.isEmpty());
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
        String context = service.buildFaqContext(null);
        assertEquals("", context);
    }

    @Test
    void shouldHandleBlankQueryInBuildFaqContext() {
        String context = service.buildFaqContext("  ");
        assertEquals("", context);
    }

    @Test
    void shouldSplitImagesFromNull() throws Exception {
        var method = FaqEmbeddingService.class.getDeclaredMethod("splitImages", String.class);
        method.setAccessible(true);
        @SuppressWarnings("unchecked")
        List<String> result = (List<String>) method.invoke(null, (String) null);
        assertTrue(result.isEmpty());
    }

    @Test
    void shouldSplitImagesFromBlank() throws Exception {
        var method = FaqEmbeddingService.class.getDeclaredMethod("splitImages", String.class);
        method.setAccessible(true);
        @SuppressWarnings("unchecked")
        List<String> result = (List<String>) method.invoke(null, "  ");
        assertTrue(result.isEmpty());
    }

    @Test
    void shouldSplitImagesFromCommaSeparated() throws Exception {
        var method = FaqEmbeddingService.class.getDeclaredMethod("splitImages", String.class);
        method.setAccessible(true);
        @SuppressWarnings("unchecked")
        List<String> result = (List<String>) method.invoke(null, "img1.jpg,img2.jpg");
        assertEquals(2, result.size());
        assertEquals("img1.jpg", result.get(0));
        assertEquals("img2.jpg", result.get(1));
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
}
