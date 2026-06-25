package com.vpnsupport.llm;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@Component
public class McpRouter {

    private static final Logger log = LoggerFactory.getLogger(McpRouter.class);

    private final List<McpClientInterface> clients;

    public McpRouter(List<McpClientInterface> clients) {
        this.clients = clients != null ? clients : List.of();
        int totalTools = listTools().size();
        log.info("McpRouter initialized with {} client(s), {} total tools", this.clients.size(), totalTools);
    }

    public List<McpTool> listTools() {
        List<McpTool> allTools = new ArrayList<>();
        for (McpClientInterface client : clients) {
            allTools.addAll(client.listTools());
        }
        return allTools;
    }

    public String callTool(String toolName, Map<String, Object> arguments) {
        for (McpClientInterface client : clients) {
            for (McpTool tool : client.listTools()) {
                if (tool.getName().equals(toolName)) {
                    return client.callTool(toolName, arguments);
                }
            }
        }
        log.warn("Unknown tool requested: {}", toolName);
        return "{\"error\": \"Unknown tool: " + toolName + "\"}";
    }
}
