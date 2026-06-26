package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class McpToolTest {

    @Test
    void shouldCreateWithDefaultConstructor() {
        McpTool tool = new McpTool();
        assertNotNull(tool);
    }

    @Test
    void shouldCreateWithParameterizedConstructor() {
        Map<String, Object> schema = Map.of("type", "object");
        McpTool tool = new McpTool("nodes_get", "Get nodes", schema);

        assertEquals("nodes_get", tool.getName());
        assertEquals("Get nodes", tool.getDescription());
        assertEquals(schema, tool.getInputSchema());
    }

    @Test
    void shouldAllowSettingName() {
        McpTool tool = new McpTool();
        tool.setName("test_tool");
        assertEquals("test_tool", tool.getName());
    }

    @Test
    void shouldAllowSettingDescription() {
        McpTool tool = new McpTool();
        tool.setDescription("A test tool");
        assertEquals("A test tool", tool.getDescription());
    }

    @Test
    void shouldAllowSettingInputSchema() {
        McpTool tool = new McpTool();
        Map<String, Object> schema = Map.of("properties", Map.of());
        tool.setInputSchema(schema);
        assertEquals(schema, tool.getInputSchema());
    }

    @Test
    void shouldAllowNullValues() {
        McpTool tool = new McpTool();
        tool.setName(null);
        tool.setDescription(null);
        tool.setInputSchema(null);

        assertNull(tool.getName());
        assertNull(tool.getDescription());
        assertNull(tool.getInputSchema());
    }
}
