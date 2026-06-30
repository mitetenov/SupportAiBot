package com.vpnsupport.llm;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@Component
public class McpRouter {

    private static final Logger log = LoggerFactory.getLogger(McpRouter.class);

    private static final java.util.Set<String> ALLOWED_TOOLS = java.util.Set.of(
            "users_get_by_telegram_id",
            "users_revoke_subscription",
            "nodes_list",
            "nodes_get",
            "hwid_devices_list",
            "hwid_device_delete"
    );

    private final List<McpClientInterface> clients;
    private final Map<String, McpClientInterface> toolToClient;
    private final ObjectMapper objectMapper;

    public McpRouter(List<McpClientInterface> clients, ObjectMapper objectMapper) {
        this.clients = clients != null ? clients : List.of();
        this.objectMapper = objectMapper;
        this.toolToClient = buildToolToClientMap();
        log.info("McpRouter initialized with {} client(s), {} total tools", this.clients.size(), listTools().size());
    }

    private Map<String, McpClientInterface> buildToolToClientMap() {
        Map<String, McpClientInterface> map = new java.util.LinkedHashMap<>();
        for (McpClientInterface client : clients) {
            for (McpTool tool : client.listTools()) {
                if (ALLOWED_TOOLS.contains(tool.name())) {
                    map.putIfAbsent(tool.name(), client);
                }
            }
        }
        return Map.copyOf(map);
    }

    public List<McpTool> listTools() {
        List<McpTool> allTools = new ArrayList<>();
        for (McpClientInterface client : clients) {
            for (McpTool tool : client.listTools()) {
                if (ALLOWED_TOOLS.contains(tool.name())) {
                    allTools.add(tool);
                }
            }
        }
        return allTools;
    }

    public String callTool(String toolName, Map<String, Object> arguments) {
        McpClientInterface client = toolToClient.get(toolName);
        if (client != null) {
            return client.callTool(toolName, arguments);
        }
        log.warn("Unknown tool requested: {}", toolName);
        try {
            return objectMapper.writeValueAsString(Map.of("error", "Unknown tool: " + toolName));
        } catch (JsonProcessingException e) {
            return "{\"error\":\"Unknown tool\"}";
        }
    }
}
