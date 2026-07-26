package com.vpnsupport.bot;

import com.vpnsupport.rag.EmbeddingProvider;
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

    @Mock
    private EmbeddingProvider embeddingProvider;

    private KnowledgeGapService service;

    @BeforeEach
    void setUp() {
        lenient().when(embeddingProvider.getDimension()).thenReturn(1536);
        service = new KnowledgeGapService(jdbcTemplate, faqEmbeddingService, embeddingProvider);
    }


    /** Builds the retrieval a caller would have passed in. */
    private static FaqEmbeddingService.FaqContext context(double maxSimilarity, String bestQuestion) {
        if (bestQuestion == null && maxSimilarity == 0.0) {
            return FaqEmbeddingService.FaqContext.EMPTY;
        }
        return new FaqEmbeddingService.FaqContext(
                "FAQ...",
                List.of(new FaqEmbeddingService.FaqResult(bestQuestion, "answer", maxSimilarity, 0.01)),
                maxSimilarity,
                bestQuestion);
    }

    @Test
    void shouldInitSchema() {
        service.initSchema();

        verify(embeddingProvider, atLeastOnce()).getDimension();
        verify(jdbcTemplate).execute("CREATE EXTENSION IF NOT EXISTS vector");
        verify(jdbcTemplate).execute(contains("CREATE TABLE IF NOT EXISTS knowledge_gaps"));
        verify(jdbcTemplate).execute(contains("VECTOR(1536)"));
    }

    @Test
    void shouldNotTriggerWhenFaqFoundWithGoodMatch() {
        service.evaluate("Как настроить VPN", 12345L, "Вот инструкция: нажмите кнопку", context(0.85, "Найден FAQ"));

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldTriggerNoMatchWhenNoFaqFound() {
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Странный вопрос без FAQ", 12345L, "Я не знаю ответа", context(0.0, null));

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"),
                any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerLowSimilarity() {
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Сложный вопрос", 12345L, "Попробуйте обновить подписку", context(0.45, "Слабый FAQ"));

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"),
                any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerEscalated() {
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Оплатил но не продлилось", 12345L, "Обратитесь в @PeipivoSalesBot [ESCALATE]", context(0.80, "FAQ об оплате"));

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"),
                any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldTriggerLlmUnsure() {
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluate("Вопрос про другое", 12345L, "К сожалению, я не знаю ответа на этот вопрос", context(0.75, "Какой-то FAQ"));

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"),
                any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldEvaluateOperatorRequest() {
        when(faqEmbeddingService.embedQueryAsVector(anyString())).thenReturn(null);

        service.evaluateOperatorRequest("Нужен оператор", 12345L, context(0.80, "FAQ"));

        verify(jdbcTemplate).update(contains("INSERT INTO knowledge_gaps"),
                any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void shouldNotEvaluateNullQuery() {
        service.evaluate(null, 12345L, "Ответ", FaqEmbeddingService.FaqContext.EMPTY);

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldNotEvaluateBlankQuery() {
        service.evaluate("  ", 12345L, "Ответ", FaqEmbeddingService.FaqContext.EMPTY);

        verify(jdbcTemplate, never()).update(anyString(), any(), any());
    }

    @Test
    void shouldNotEvaluateNullOperatorQuery() {
        service.evaluateOperatorRequest(null, 12345L, FaqEmbeddingService.FaqContext.EMPTY);

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
