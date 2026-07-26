package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.RemnawaveMcpProperties;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class McpRouterTest {

    private static final long CALLER = 555_000L;

    private final ObjectMapper objectMapper = new ObjectMapper();

    private static RemnawaveMcpProperties props(boolean readonly) {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setReadonly(readonly);
        return properties;
    }

    private McpRouter router(List<McpClientInterface> clients) {
        return new McpRouter(clients, objectMapper, props(false));
    }

    @Test
    void shouldReturnEmptyToolsWithNoClients() {
        assertTrue(router(List.of()).listTools().isEmpty());
    }

    @Test
    void shouldReturnEmptyToolsWithNullClientList() {
        assertTrue(router(null).listTools().isEmpty());
    }

    @Test
    void shouldAggregateToolsFromAllClients() {
        McpClientInterface client1 = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc1", Map.of())),
                Map.of("users_get_by_telegram_id", "result1")
        );
        McpClientInterface client2 = new StubMcpClient(
                List.of(new McpTool("hwid_devices_list", "desc2", Map.of())),
                Map.of("hwid_devices_list", "result2")
        );

        List<McpTool> tools = router(List.of(client1, client2)).listTools();

        assertEquals(2, tools.size());
        assertEquals("users_get_by_telegram_id", tools.get(0).name());
        assertEquals("hwid_devices_list", tools.get(1).name());
    }

    @Test
    void shouldCallCorrectTool() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("nodes_list", "A test tool", Map.of())),
                Map.of("nodes_list", "{\"status\": \"ok\"}")
        );

        String result = router(List.of(client)).callTool("nodes_list", Map.of("param", "value"), CALLER);

        assertTrue(result.contains("ok"));
    }

    @Test
    void shouldReturnErrorForUnknownTool() {
        String result = router(List.of()).callTool("nonexistent", Map.of(), CALLER);

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
                List.of(new McpTool("hwid_devices_list", "desc2", Map.of())),
                Map.of("hwid_devices_list", "result2")
        );

        McpRouter router = router(List.of(client1, client2));

        assertEquals("result1", router.callTool("users_get_by_telegram_id", Map.of(), CALLER));
        assertEquals("result2", router.callTool("hwid_devices_list", Map.of(), CALLER));
    }

    @Test
    void shouldReturnErrorForToolNotInAnyClient() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("nodes_get", "desc", Map.of())),
                Map.of("nodes_get", "result_a")
        );

        String result = router(List.of(client)).callTool("hwid_devices_list", Map.of(), CALLER);

        assertTrue(result.contains("hwid_devices_list"));
    }

    @Test
    void shouldHandleEmptyClientsWithNullSafety() {
        McpRouter router = router(null);
        assertTrue(router.listTools().isEmpty());
        assertTrue(router.callTool("any", Map.of(), CALLER).contains("error"));
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

        List<McpTool> tools = router(List.of(client)).listTools();

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

        String result = router(List.of(client)).callTool("some_unsafe_tool", Map.of(), CALLER);

        assertTrue(result.contains("error"));
        assertTrue(result.contains("some_unsafe_tool"));
    }

    @Test
    void shouldRejectToolNotInAnyClientEvenIfResultExists() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc", Map.of())),
                Map.of("users_get_by_telegram_id", "result")
        );

        String result = router(List.of(client)).callTool("nonexistent_tool", Map.of(), CALLER);

        assertTrue(result.contains("error"));
        assertTrue(result.contains("nonexistent_tool"));
    }

    @Test
    void shouldNeverExposeRevokeSubscription() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("users_revoke_subscription", "destructive", Map.of())),
                Map.of("users_revoke_subscription", "revoked!")
        );

        McpRouter router = router(List.of(client));

        assertTrue(router.listTools().isEmpty());
        String result = router.callTool("users_revoke_subscription", Map.of(), CALLER);
        assertTrue(result.contains("error"));
        assertFalse(result.contains("revoked!"));
    }

    @Test
    void shouldWithholdWriteToolsInReadonlyMode() {
        McpClientInterface client = new StubMcpClient(
                List.of(
                        new McpTool("hwid_devices_list", "read", Map.of()),
                        new McpTool("hwid_device_delete", "write", Map.of())
                ),
                Map.of("hwid_device_delete", "deleted!")
        );

        McpRouter readonly = new McpRouter(List.of(client), objectMapper, props(true));

        assertEquals(1, readonly.listTools().size());
        assertEquals("hwid_devices_list", readonly.listTools().get(0).name());

        String result = readonly.callTool("hwid_device_delete", Map.of("hwid", "x"), CALLER);
        assertTrue(result.contains("Tool not allowed"));
        assertFalse(result.contains("deleted!"));
    }

    @Test
    void shouldExposeWriteToolsWhenNotReadonly() {
        McpClientInterface client = new StubMcpClient(
                List.of(new McpTool("hwid_device_delete", "write", Map.of())),
                Map.of("hwid_device_delete", "deleted!")
        );

        McpRouter router = router(List.of(client));

        assertEquals(1, router.listTools().size());
        assertTrue(router.callTool("hwid_device_delete", Map.of("hwid", "x"), CALLER).contains("deleted!"));
    }

    @Test
    void shouldOverrideTelegramIdSuppliedByTheModel() {
        StubMcpClient client = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc", Map.of())),
                Map.of("users_get_by_telegram_id", "ok")
        );

        router(List.of(client)).callTool(
                "users_get_by_telegram_id", Map.of("telegramId", 999_999L), CALLER);

        assertEquals(CALLER, client.lastArguments().get("telegramId"));
    }

    @Test
    void shouldOverrideSnakeCaseTelegramIdVariant() {
        StubMcpClient client = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc", Map.of())),
                Map.of("users_get_by_telegram_id", "ok")
        );

        router(List.of(client)).callTool(
                "users_get_by_telegram_id", Map.of("telegram_id", "999999"), CALLER);

        assertEquals(CALLER, client.lastArguments().get("telegram_id"));
    }

    @Test
    void shouldSupplyTelegramIdFromSchemaWhenModelOmitsIt() {
        StubMcpClient client = new StubMcpClient(
                List.of(new McpTool("users_get_by_telegram_id", "desc",
                        Map.of("type", "object", "properties", Map.of("telegramId", Map.of("type", "number"))))),
                Map.of("users_get_by_telegram_id", "ok")
        );

        router(List.of(client)).callTool("users_get_by_telegram_id", Map.of(), CALLER);

        assertEquals(CALLER, client.lastArguments().get("telegramId"));
    }

    @Test
    void shouldLeaveUnrelatedArgumentsUntouched() {
        StubMcpClient client = new StubMcpClient(
                List.of(new McpTool("nodes_get", "desc", Map.of())),
                Map.of("nodes_get", "ok")
        );

        router(List.of(client)).callTool("nodes_get", Map.of("uuid", "abc-123"), CALLER);

        assertEquals("abc-123", client.lastArguments().get("uuid"));
        assertFalse(client.lastArguments().containsKey("telegramId"));
    }

    @Test
    void shouldTolerateNullArguments() {
        StubMcpClient client = new StubMcpClient(
                List.of(new McpTool("nodes_list", "desc", Map.of())),
                Map.of("nodes_list", "ok")
        );

        assertEquals("ok", router(List.of(client)).callTool("nodes_list", null, CALLER));
        assertTrue(client.lastArguments().isEmpty());
    }

    private static class StubMcpClient implements McpClientInterface {
        private final List<McpTool> tools;
        private final Map<String, String> toolResults;
        private final List<Map<String, Object>> calls = new ArrayList<>();

        StubMcpClient(List<McpTool> tools, Map<String, String> toolResults) {
            this.tools = tools;
            this.toolResults = toolResults;
        }

        Map<String, Object> lastArguments() {
            return calls.isEmpty() ? Map.of() : calls.get(calls.size() - 1);
        }

        @Override
        public List<McpTool> listTools() {
            return tools;
        }

        @Override
        public String callTool(String toolName, Map<String, Object> arguments) {
            calls.add(arguments);
            return toolResults.getOrDefault(toolName, "{\"error\": \"Unknown\"}");
        }
    }
}
