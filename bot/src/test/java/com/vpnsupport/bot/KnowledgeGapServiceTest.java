package com.vpnsupport.bot;

import com.vpnsupport.rag.FaqEmbeddingService;
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
class KnowledgeGapServiceTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    private KnowledgeGapService service;

    @BeforeEach
    void setUp() {
        service = new KnowledgeGapService(jdbcTemplate, faqEmbeddingService);
    }

    @Test
    void shouldInitSchema() {
        service.initSchema();

        verify(jdbcTemplate).execute("CREATE EXTENSION IF NOT EXISTS vector");
        verify(jdbcTemplate).execute(contains("CREATE TABLE IF NOT EXISTS knowledge_gaps"));
    }

    @Test
    void shouldNotTriggerWhenFaqFoundWithGoodMatch() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.85);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn("Найден FAQ");

        service.evaluate("Как настроить VPN", 12345L, "Вот инструкция: нажмите кнопку");

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldTriggerNoMatchWhenNoFaqFound() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.0);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn(null);
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Странный вопрос без FAQ", 12345L, "Я не знаю ответа");

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"), any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerLowSimilarity() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.45);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn("Слабый FAQ");
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Сложный вопрос", 12345L, "Попробуйте обновить подписку");

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"), any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerEscalated() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.80);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn("FAQ об оплате");
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Оплатил но не продлилось", 12345L, "Обратитесь в @PeipivoSalesBot [ESCALATE]");

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"), any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerLlmUnsure() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.75);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn("Какой-то FAQ");
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Вопрос про другое", 12345L, "К сожалению, я не знаю ответа на этот вопрос");

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"), any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldEvaluateOperatorRequest() {
        when(faqEmbeddingService.getLastMaxSimilarity()).thenReturn(0.80);
        when(faqEmbeddingService.getLastFaqQuestion()).thenReturn("FAQ");
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluateOperatorRequest("Нужен оператор", 12345L);

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"), any(), any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldNotEvaluateNullQuery() {
        service.evaluate(null, 12345L, "Ответ");

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldNotEvaluateBlankQuery() {
        service.evaluate("  ", 12345L, "Ответ");

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldNotEvaluateNullOperatorQuery() {
        service.evaluateOperatorRequest(null, 12345L);

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldGetTopGaps() {
        List<GapStatsDto> gaps = service.getTopGaps();

        assertTrue(gaps.isEmpty());
    }

    @Test
    void shouldGetTopGapsWithLimit() {
        List<GapStatsDto> gaps = service.getTopGaps(5);

        assertTrue(gaps.isEmpty());
    }
}
