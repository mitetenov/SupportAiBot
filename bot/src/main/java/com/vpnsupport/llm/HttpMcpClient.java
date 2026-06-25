package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.HappMcpProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import jakarta.annotation.PostConstruct;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;

@Component
@ConditionalOnProperty(name = "happ.mcp.enabled", havingValue = "true", matchIfMissing = true)
public class HttpMcpClient implements McpClientInterface {

    private static final Logger log = LoggerFactory.getLogger(HttpMcpClient.class);
    private static final String PROTOCOL_VERSION = "2024-11-05";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);

    private final WebClient webClient;
    private final ObjectMapper objectMapper;
    private final AtomicInteger requestId = new AtomicInteger(0);
    private volatile List<McpTool> cachedTools = Collections.emptyList();
    private volatile boolean initialized = false;

    public HttpMcpClient(HappMcpProperties properties, ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
        this.webClient = WebClient.builder()
                .baseUrl(properties.getUrl())
                .defaultHeader("Content-Type", "application/json")
                .defaultHeader("Accept", "application/json, text/event-stream")
                .build();
    }

    @PostConstruct
    public void init() {
        try {
            initializeProtocol();
            loadTools();
            initialized = true;
            log.info("Happ MCP client initialized with {} tools", cachedTools.size());
        } catch (Exception e) {
            log.error("Failed to initialize Happ MCP client", e);
        }
    }

    private void initializeProtocol() throws Exception {
        Map<String, Object> params = Map.of(
                "protocolVersion", PROTOCOL_VERSION,
                "capabilities", Map.of(),
                "clientInfo", Map.of("name", "vpn-support-bot", "version", "1.0.0")
        );
        String response = sendJsonRpc("initialize", params);
        log.info("Happ MCP initialize response: {}", response);
    }

    private void loadTools() throws Exception {
        String response = sendJsonRpc("tools/list", Map.of());
        JsonNode json = objectMapper.readTree(response);
        JsonNode tools = json.get("tools");
        if (tools != null && tools.isArray()) {
            List<McpTool> toolList = new ArrayList<>();
            for (JsonNode tool : tools) {
                String name = tool.get("name").asText();
                String description = tool.has("description") ? tool.get("description").asText() : "";
                @SuppressWarnings("unchecked")
                Map<String, Object> inputSchema = tool.has("inputSchema")
                        ? objectMapper.convertValue(tool.get("inputSchema"), Map.class)
                        : Map.of();
                toolList.add(new McpTool(name, description, inputSchema));
            }
            cachedTools = Collections.unmodifiableList(toolList);
        }
    }

    @Override
    public List<McpTool> listTools() {
        return cachedTools;
    }

    @Override
    public String callTool(String toolName, Map<String, Object> arguments) {
        if (!initialized) {
            return "{\"error\": \"Happ MCP client not initialized\"}";
        }
        try {
            return sendJsonRpc("tools/call", Map.of(
                    "name", toolName,
                    "arguments", arguments
            ));
        } catch (Exception e) {
            log.error("Failed to call Happ tool: {}", toolName, e);
            return "{\"error\": \"" + e.getMessage() + "\"}";
        }
    }

    private String sendJsonRpc(String method, Map<String, Object> params) throws Exception {
        int id = requestId.incrementAndGet();

        Map<String, Object> request = new LinkedHashMap<>();
        request.put("jsonrpc", "2.0");
        request.put("id", id);
        request.put("method", method);
        request.put("params", params != null ? params : Map.of());

        String requestJson = objectMapper.writeValueAsString(request);

        String sseBody = webClient.post()
                .bodyValue(requestJson)
                .retrieve()
                .bodyToMono(String.class)
                .block(REQUEST_TIMEOUT);

        if (sseBody == null || sseBody.isBlank()) {
            throw new RuntimeException("Empty response from Happ MCP");
        }

        String dataLine = null;
        for (String line : sseBody.split("\n")) {
            if (line.startsWith("data: ")) {
                dataLine = line.substring(6);
                break;
            }
        }

        if (dataLine == null) {
            throw new RuntimeException("No data line in SSE response: " + sseBody);
        }

        JsonNode json = objectMapper.readTree(dataLine);
        if (json.has("error")) {
            throw new RuntimeException(json.get("error").toString());
        }

        return objectMapper.writeValueAsString(json.get("result"));
    }
}
