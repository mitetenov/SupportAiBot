package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class McpRouterTest {

    private final ObjectMapper objectMapper = new ObjectMapper();

    @Test
    void shouldReturnEmptyToolsWithNoClients() {
        McpRouter router = new McpRouter(List.of(), objectMapper);
        List<McpTool> tools = router.listTools();
        assertTrue(tools.isEmpty());
    }

    @Test
    void shouldReturnEmptyToolsWithNullClientList() {
        McpRouter router = new McpRouter(null, objectMapper);
        List<McpTool> tools = router.listTools();
        assertTrue(tools.isEmpty());
    }

    @Test
    void shouldAggregateToolsFromAllClients() {
        McpClientInterface client1 = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc1", Map.of())),
                Map.of("users_get_by_telegram_id", "result1")
        );
        McpClientInterface client2 = new StubMcpClient(
                List.of(new McpTool("users_revoke_subscription", "desc2", Map.of())),
                Map.of("users_revoke_subscription", "result2")
        );

        McpRouter router = new McpRouter(List.of(client1, client2), objectMapper);
        List<McpTool> tools = router.listTools();

        assertEquals(2, tools.size());
        assertEquals("users_get_by_telegram_id", tools.get(0).name());
        assertEquals("users_revoke_subscription", tools.get(1).name());
    }

    @Test
    void shouldCallCorrectTool() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("nodes_list", "A test tool", Map.of())),
                Map.of("nodes_list", "{\"status\": \"ok\"}")
        );

        McpRouter router = new McpRouter(List.of(client), objectMapper);
        String result = router.callTool("nodes_list", Map.of("param", "value"));

        assertTrue(result.contains("ok"));
    }

    @Test
    void shouldReturnErrorForUnknownTool() {
        McpRouter router = new McpRouter(List.of(), objectMapper);
        String result = router.callTool("nonexistent", Map.of());

        assertTrue(result.contains("error"));
        assertTrue(result.contains("nonexistent"));
    }

    @Test
    void shouldCallToolFromCorrectClientWhenMultiple() {
        McpClientInterface client1 = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc1", Map.of())),
                Map.of("users_get_by_telegram_id", "result1")
        );
        McpClientInterface client2 = new StubMcpClient(
                List.of(new McpTool("users_revoke_subscription", "desc2", Map.of())),
                Map.of("users_revoke_subscription", "result2")
        );

        McpRouter router = new McpRouter(List.of(client1, client2), objectMapper);

        assertEquals("result1", router.callTool("users_get_by_telegram_id", Map.of()));
        assertEquals("result2", router.callTool("users_revoke_subscription", Map.of()));
    }

    @Test
    void shouldReturnErrorForToolNotInAnyClient() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("nodes_get", "desc", Map.of())),
                Map.of("nodes_get", "result_a")
        );

        McpRouter router = new McpRouter(List.of(client), objectMapper);
        String result = router.callTool("hwid_devices_list", Map.of());

        assertTrue(result.contains("hwid_devices_list"));
    }

    @Test
    void shouldHandleEmptyClientsWithNullSafety() {
        McpRouter router = new McpRouter(null, objectMapper);
        assertTrue(router.listTools().isEmpty());
        assertTrue(router.callTool("any", Map.of()).contains("error"));
    }

    @Test
    void shouldFilterOutNonAllowedTools() {
        McpClientInterface client = new StubMcpClient(
                List.of(
                        new McpTool("users_get_by_telegram_id", "Allowed tool", Map.of()),
                        new McpTool("some_unsafe_tool", "Should be filtered out", Map.of()),
                        new McpTool("nodes_list", "Also allowed", Map.of())
                ),
                Map.of(
                        "users_get_by_telegram_id", "allowed_result",
                        "some_unsafe_tool", "should_not_be_callable"
                )
        );

        McpRouter router = new McpRouter(List.of(client), objectMapper);
        List<McpTool> tools = router.listTools();

        assertEquals(2, tools.size());
        assertEquals("users_get_by_telegram_id", tools.get(0).name());
        assertEquals("nodes_list", tools.get(1).name());
    }

    @Test
    void shouldReturnErrorForFilteredTool() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("some_unsafe_tool", "Filtered tool", Map.of())),
                Map.of("some_unsafe_tool", "should_not_be_callable")
        );

        McpRouter router = new McpRouter(List.of(client), objectMapper);
        String result = router.callTool("some_unsafe_tool", Map.of());

        assertTrue(result.contains("error"));
        assertTrue(result.contains("some_unsafe_tool"));
    }

    @Test
    void shouldRejectToolNotInAnyClientEvenIfResultExists() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc", Map.of())),
                Map.of("users_get_by_telegram_id", "result")
        );

        McpRouter router = new McpRouter(List.of(client), objectMapper);
        String result = router.callTool("nonexistent_tool", Map.of());

        assertTrue(result.contains("error"));
        assertTrue(result.contains("nonexistent_tool"));
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
