package com.vpnsupport.bot;

import com.vpnsupport.config.ChatHistoryProperties;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.task.TaskExecutor;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

@ExtendWith(MockitoExtension.class)
class ChatHistoryServiceTest {

    @Mock
    private ChatMessageRepository chatMessageRepository;

    @Mock
    private TaskExecutor taskExecutor;

    private ChatHistoryService service;

    @BeforeEach
    void setUp() {
        ChatHistoryProperties properties = new ChatHistoryProperties();
        properties.setMaxMessages(20);
        properties.setTtlDays(7);
        service = new ChatHistoryService(chatMessageRepository, taskExecutor, properties);
    }

    @Test
    void shouldReturnEmptyHistoryForUnknownUser() {
        List<Map<String, Object>> history = service.getHistory(1L);
        assertTrue(history.isEmpty());
    }

    @Test
    void shouldAddUserMessage() {
        service.addUserMessage(1L, "Hello");
        List<Map<String, Object>> history = service.getHistory(1L);

        assertEquals(1, history.size());
        assertEquals("user", history.get(0).get("role"));
        assertEquals("Hello", history.get(0).get("content"));
    }

    @Test
    void shouldAddAssistantMessage() {
        service.addAssistantMessage(1L, "Hi there!");
        List<Map<String, Object>> history = service.getHistory(1L);

        assertEquals(1, history.size());
        assertEquals("assistant", history.get(0).get("role"));
        assertEquals("Hi there!", history.get(0).get("content"));
    }

    @Test
    void shouldMaintainOrder() {
        service.addUserMessage(1L, "Q1");
        service.addAssistantMessage(1L, "A1");
        service.addUserMessage(1L, "Q2");
        service.addAssistantMessage(1L, "A2");

        List<Map<String, Object>> history = service.getHistory(1L);
        assertEquals(4, history.size());
        assertEquals("Q1", history.get(0).get("content"));
        assertEquals("A1", history.get(1).get("content"));
        assertEquals("Q2", history.get(2).get("content"));
        assertEquals("A2", history.get(3).get("content"));
    }

    @Test
    void shouldRotateOldMessagesBeyondMax() {
        for (int i = 0; i < 30; i++) {
            service.addUserMessage(1L, "msg" + i);
            service.addAssistantMessage(1L, "rsp" + i);
        }

        List<Map<String, Object>> history = service.getHistory(1L);
        assertEquals(20, history.size());
        assertEquals("msg20", history.get(0).get("content"));
        assertEquals("rsp29", history.get(19).get("content"));
    }

    @Test
    void shouldClearHistory() {
        service.addUserMessage(1L, "Hello");
        service.addAssistantMessage(1L, "Hi");

        service.clear(1L);

        List<Map<String, Object>> history = service.getHistory(1L);
        assertTrue(history.isEmpty());
    }

    @Test
    void shouldKeepUsersSeparate() {
        service.addUserMessage(1L, "U1");
        service.addUserMessage(2L, "U2");

        List<Map<String, Object>> h1 = service.getHistory(1L);
        List<Map<String, Object>> h2 = service.getHistory(2L);

        assertEquals(1, h1.size());
        assertEquals(1, h2.size());
        assertEquals("U1", h1.get(0).get("content"));
        assertEquals("U2", h2.get(0).get("content"));
    }

    @Test
    void clearShouldOnlyAffectSpecifiedUser() {
        service.addUserMessage(1L, "U1");
        service.addUserMessage(2L, "U2");

        service.clear(1L);

        assertTrue(service.getHistory(1L).isEmpty());
        assertEquals(1, service.getHistory(2L).size());
    }

    @Test
    void shouldConvertToGeminiContents() {
        service.addUserMessage(1L, "Hello");
        service.addAssistantMessage(1L, "Hi");

        List<Map<String, Object>> contents = service.toGeminiContents(1L);

        assertEquals(2, contents.size());
        assertEquals("user", contents.get(0).get("role"));
        assertEquals("model", contents.get(1).get("role"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> parts0 = (List<Map<String, Object>>) contents.get(0).get("parts");
        assertEquals("Hello", parts0.get(0).get("text"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> parts1 = (List<Map<String, Object>>) contents.get(1).get("parts");
        assertEquals("Hi", parts1.get(0).get("text"));
    }

    @Test
    void toGeminiContentsShouldReturnEmptyForUnknownUser() {
        List<Map<String, Object>> contents = service.toGeminiContents(999L);
        assertTrue(contents.isEmpty());
    }

    @Test
    void shouldReturnImmutableCopy() {
        service.addUserMessage(1L, "Hello");
        List<Map<String, Object>> history = service.getHistory(1L);

        try {
            history.add(Map.of("role", "user", "content", "injected"));
        } catch (UnsupportedOperationException expected) {
        }

        assertEquals(1, service.getHistory(1L).size());
    }

    @Test
    void shouldNotThrowForNullContent() {
        assertDoesNotThrow(() -> service.addUserMessage(1L, null));
        assertDoesNotThrow(() -> service.addAssistantMessage(1L, null));
    }
}
