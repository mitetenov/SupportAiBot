package com.vpnsupport.llm;

import java.util.Map;

public record McpTool(String name, String description, Map<String, Object> inputSchema) {

    public McpTool {
        if (inputSchema == null) {
            inputSchema = Map.of();
        }
    }
}
