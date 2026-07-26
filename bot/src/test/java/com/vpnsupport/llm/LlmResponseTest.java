package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Only {@code hasToolCalls} carries logic — the rest of the record is compiler
 * generated and not worth asserting on.
 */
class LlmResponseTest {

    @Test
    void shouldTreatANullToolCallListAsNoToolCalls() {
        assertFalse(new LlmResponse("text", null, List.of()).hasToolCalls());
    }

    @Test
    void shouldTreatAnEmptyToolCallListAsNoToolCalls() {
        assertFalse(new LlmResponse("text").hasToolCalls());
    }

    @Test
    void shouldDetectToolCalls() {
        LlmResponse response = new LlmResponse("", List.of(
                new LlmResponse.ToolCall("nodes_list", "id1", Map.of())));
        assertTrue(response.hasToolCalls());
    }
}
