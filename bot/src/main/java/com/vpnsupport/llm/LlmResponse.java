package com.vpnsupport.llm;

import java.util.Collections;
import java.util.List;
import java.util.Map;

public record LlmResponse(String text, List<ToolCall> toolCalls) {

    public LlmResponse(String text) {
        this(text, Collections.emptyList());
    }

    public boolean hasToolCalls() {
        return toolCalls != null && !toolCalls.isEmpty();
    }

    public record ToolCall(String name, String id, Map<String, Object> arguments) {}
}
