package com.vpnsupport.llm;

import java.util.Collections;
import java.util.List;
import java.util.Map;

public record LlmResponse(String text, List<ToolCall> toolCalls, List<Map<String, Object>> rawParts) {

    public LlmResponse(String text, List<ToolCall> toolCalls) {
        this(text, toolCalls, Collections.emptyList());
    }

    public LlmResponse(String text) {
        this(text, Collections.emptyList(), Collections.emptyList());
    }

    public boolean hasToolCalls() {
        return toolCalls != null && !toolCalls.isEmpty();
    }

    public record ToolCall(String name, String id, Map<String, Object> arguments, String thoughtSignature) {
        public ToolCall(String name, String id, Map<String, Object> arguments) {
            this(name, id, arguments, null);
        }
    }
}
