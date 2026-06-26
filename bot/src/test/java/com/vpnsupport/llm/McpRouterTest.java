package com.vpnsupport.llm;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class McpRouterTest {

    @Test
    void shouldReturnEmptyToolsWithNoClients() {
        McpRouter router = new McpRouter(List.of());
        List<McpTool> tools = router.listTools();
        assertTrue(tools.isEmpty());
    }

    @Test
    void shouldReturnEmptyToolsWithNullClientList() {
        McpRouter router = new McpRouter(null);
        List<McpTool> tools = router.listTools();
        assertTrue(tools.isEmpty());
    }

    @Test
    void shouldAggregateToolsFromAllClients() {
        McpClientInterface client1 = new StubMcpClient(
                List.of(new McpTool("tool1", "desc1", Map.of())),
                Map.of("tool1", "result1")
        );
        McpClientInterface client2 = new StubMcpClient(
                List.of(new McpTool("tool2", "desc2", Map.of())),
                Map.of("tool2", "result2")
        );

        McpRouter router = new McpRouter(List.of(client1, client2));
        List<McpTool> tools = router.listTools();

        assertEquals(2, tools.size());
        assertEquals("tool1", tools.get(0).getName());
        assertEquals("tool2", tools.get(1).getName());
    }

    @Test
    void shouldCallCorrectTool() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("test_tool", "A test tool", Map.of())),
                Map.of("test_tool", "{\"status\": \"ok\"}")
        );

        McpRouter router = new McpRouter(List.of(client));
        String result = router.callTool("test_tool", Map.of("param", "value"));

        assertTrue(result.contains("ok"));
    }

    @Test
    void shouldReturnErrorForUnknownTool() {
        McpRouter router = new McpRouter(List.of());
        String result = router.callTool("nonexistent", Map.of());

        assertTrue(result.contains("error"));
        assertTrue(result.contains("Unknown tool"));
        assertTrue(result.contains("nonexistent"));
    }

    @Test
    void shouldCallToolFromCorrectClientWhenMultiple() {
        McpClientInterface client1 = new StubMcpClient(
                List.of(new McpTool("tool1", "desc1", Map.of())),
                Map.of("tool1", "result1")
        );
        McpClientInterface client2 = new StubMcpClient(
                List.of(new McpTool("tool2", "desc2", Map.of())),
                Map.of("tool2", "result2")
        );

        McpRouter router = new McpRouter(List.of(client1, client2));

        assertEquals("result1", router.callTool("tool1", Map.of()));
        assertEquals("result2", router.callTool("tool2", Map.of()));
    }

    @Test
    void shouldReturnErrorForToolNotInAnyClient() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("tool_a", "desc", Map.of())),
                Map.of("tool_a", "result_a")
        );

        McpRouter router = new McpRouter(List.of(client));
        String result = router.callTool("tool_b", Map.of());

        assertTrue(result.contains("Unknown tool: tool_b"));
    }

    @Test
    void shouldHandleEmptyClientsWithNullSafety() {
        McpRouter router = new McpRouter(null);
        assertTrue(router.listTools().isEmpty());
        assertTrue(router.callTool("any", Map.of()).contains("error"));
    }

    private static class StubMcpClient implements McpClientInterface {
        private final List<McpTool> tools;
        private final Map<String, String> toolResults;

        StubMcpClient(List<McpTool> tools, Map<String, String> toolResults) {
            this.tools = tools;
            this.toolResults = toolResults;
        }

        @Override
        public List<McpTool> listTools() {
            return tools;
        }

        @Override
        public String callTool(String toolName, Map<String, Object> arguments) {
            return toolResults.getOrDefault(toolName, "{\"error\": \"Unknown\"}");
        }
    }
}
