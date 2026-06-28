package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

class McpToolTest {

    @Test
    void shouldCreateWithConstructor() {
        Map<String, Object> schema = Map.of("type", "object");
        McpTool tool = new McpTool("nodes_get", "Get nodes", schema);

        assertEquals("nodes_get", tool.name());
        assertEquals("Get nodes", tool.description());
        assertEquals(schema, tool.inputSchema());
    }

    @Test
    void shouldCreateWithNullSchema() {
        McpTool tool = new McpTool("test", "desc", null);

        assertEquals("test", tool.name());
        assertEquals("desc", tool.description());
        assertNotNull(tool.inputSchema());
        assertEquals(Map.of(), tool.inputSchema());
    }

    @Test
    void shouldCreateWithNullDescription() {
        McpTool tool = new McpTool("test", null, Map.of());
        assertEquals("test", tool.name());
        assertEquals(null, tool.description());
    }

    @Test
    void shouldSupportEquality() {
        McpTool a = new McpTool("t", "d", Map.of("k", "v"));
        McpTool b = new McpTool("t", "d", Map.of("k", "v"));
        assertEquals(a, b);
        assertEquals(a.hashCode(), b.hashCode());
    }

    @Test
    void shouldSupportToString() {
        McpTool tool = new McpTool("t", "d", Map.of());
        String str = tool.toString();
        assertNotNull(str);
        assertTrue(str.contains("t"));
    }

    private void assertTrue(boolean condition) {
        if (!condition) throw new AssertionError("Expected true");
    }
}
