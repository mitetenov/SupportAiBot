package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class LlmResponseTest {

    @Test
    void shouldConstructWithTextOnly() {
        LlmResponse r = new LlmResponse("Hello");
        assertEquals("Hello", r.text());
        assertTrue(r.toolCalls().isEmpty());
        assertTrue(r.rawParts().isEmpty());
        assertFalse(r.hasToolCalls());
    }

    @Test
    void shouldConstructWithTextAndToolCalls() {
        List<LlmResponse.ToolCall> calls = List.of(
                new LlmResponse.ToolCall("t1", "id1", Map.of()),
                new LlmResponse.ToolCall("t2", "id2", Map.of("k", "v"))
        );
        LlmResponse r = new LlmResponse("text", calls);
        assertEquals("text", r.text());
        assertEquals(2, r.toolCalls().size());
        assertTrue(r.rawParts().isEmpty());
        assertTrue(r.hasToolCalls());
    }

    @Test
    void shouldConstructWithAllFields() {
        List<LlmResponse.ToolCall> calls = List.of(
                new LlmResponse.ToolCall("t1", "id1", Map.of(), "sig1")
        );
        List<Map<String, Object>> rawParts = List.of(Map.of("text", "hi"));
        LlmResponse r = new LlmResponse("text", calls, rawParts);
        assertEquals("text", r.text());
        assertEquals(1, r.toolCalls().size());
        assertEquals(1, r.rawParts().size());
        assertTrue(r.hasToolCalls());
    }

    @Test
    void shouldConstructToolCallWithAllFields() {
        LlmResponse.ToolCall tc = new LlmResponse.ToolCall(
                "nodes_list", "call_1", Map.of("countryCode", "DE"), "sig_123");
        assertEquals("nodes_list", tc.name());
        assertEquals("call_1", tc.id());
        assertEquals("DE", tc.arguments().get("countryCode"));
        assertEquals("sig_123", tc.thoughtSignature());
    }

    @Test
    void shouldConstructToolCallWithoutThoughtSignature() {
        LlmResponse.ToolCall tc = new LlmResponse.ToolCall(
                "nodes_list", "call_1", Map.of());
        assertEquals("nodes_list", tc.name());
        assertEquals("call_1", tc.id());
        assertNull(tc.thoughtSignature());
    }

    @Test
    void shouldDetectHasToolCallsWhenNull() {
        LlmResponse r = new LlmResponse("text", null, List.of());
        assertFalse(r.hasToolCalls());
    }
}
