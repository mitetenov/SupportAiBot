package com.vpnsupport.llm;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.AdminNotifier;
import com.vpnsupport.config.RemnawaveMcpProperties;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * MCP client that connects to a remote MCP server via HTTP using the Streamable HTTP transport.
 * Replaces StdioMcpClient when the MCP server runs as a separate container.
 */
@Component
@ConditionalOnProperty(name = "remnawave.mcp.url")
public class HttpMcpClient implements McpClientInterface {

    private static final Logger log = LoggerFactory.getLogger(HttpMcpClient.class);
    private static final String PROTOCOL_VERSION = "2024-11-05";
    private static final long REQUEST_TIMEOUT_MS = 30_000;

    private final RemnawaveMcpProperties properties;
    private final ObjectMapper objectMapper;
    private final AdminNotifier adminNotifier;
    private final WebClient webClient;

    private volatile boolean initialized = false;
    private volatile List<McpTool> cachedTools = Collections.emptyList();
    private final AtomicInteger requestId = new AtomicInteger(0);
    private volatile String sessionId;

    public HttpMcpClient(RemnawaveMcpProperties properties,
                         ObjectMapper objectMapper,
                         AdminNotifier adminNotifier) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.adminNotifier = adminNotifier;

        HttpClient httpClient = HttpClient.create()
                .responseTimeout(Duration.ofMillis(REQUEST_TIMEOUT_MS));
        this.webClient = WebClient.builder()
                .baseUrl(properties.getUrl())
                .defaultHeader("Content-Type", "application/json")
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(httpClient))
                .build();
    }

    @PostConstruct
    public void init() {
        try {
            initializeProtocol();
            loadTools();
            initialized = true;
            log.info("HTTP MCP client initialized with {} tools from {}", cachedTools.size(), properties.getUrl());
        } catch (Exception e) {
            log.error("Failed to initialize HTTP MCP client — bot will run without Remnawave tools", e);
            adminNotifier.notifyError("MCP init failed", e);
        }
    }

    @PreDestroy
    public void shutdown() {
        initialized = false;
        log.info("HTTP MCP client shut down");
    }

    /**
     * Sends a JSON-RPC 2.0 request and returns the response.
     */
    private JsonNode sendJsonRpc(String method, Map<String, Object> params) throws Exception {
        int id = requestId.updateAndGet(i -> (i + 1) & 0x7FFFFFFF);

        Map<String, Object> request = new LinkedHashMap<>();
        request.put("jsonrpc", "2.0");
        request.put("id", id);
        request.put("method", method);
        request.put("params", params != null ? params : Map.of());

        String json = objectMapper.writeValueAsString(request);
        log.debug("MCP HTTP request [{}]: {} {}", id, method, params != null ? params.keySet() : "{}");

        // Build request with session ID if available
        WebClient.RequestHeadersSpec<?> spec = webClient.post()
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(json);

        if (sessionId != null) {
            spec = spec.header("Mcp-Session-Id", sessionId);
        }

        String responseBody = spec.retrieve()
                .toEntity(String.class)
                .map(entity -> {
                    // Capture session ID from header on first response
                    List<String> sessionHeaders = entity.getHeaders().get("Mcp-Session-Id");
                    if (sessionHeaders != null && !sessionHeaders.isEmpty() && sessionId == null) {
                        sessionId = sessionHeaders.getFirst();
                        log.debug("MCP session ID: {}", sessionId);
                    }
                    return entity.getBody();
                })
                .block(Duration.ofMillis(REQUEST_TIMEOUT_MS));

        if (responseBody == null) {
            throw new RuntimeException("Empty response from MCP server");
        }

        JsonNode response = objectMapper.readTree(responseBody);
        if (response.has("error")) {
            JsonNode error = response.get("error");
            String message = error.has("message") ? error.get("message").asText() : "unknown error";
            throw new RuntimeException("MCP error [" + method + "]: " + message);
        }

        return response.get("result");
    }

    /**
     * Sends a JSON-RPC notification (no id, fire-and-forget).
     */
    private void sendNotification(String method, Map<String, Object> params) {
        try {
            Map<String, Object> notification = new LinkedHashMap<>();
            notification.put("jsonrpc", "2.0");
            notification.put("method", method);
            notification.put("params", params != null ? params : Map.of());

            String json = objectMapper.writeValueAsString(notification);

            WebClient.RequestHeadersSpec<?> spec = webClient.post()
                    .contentType(MediaType.APPLICATION_JSON)
                    .bodyValue(json);

            if (sessionId != null) {
                spec = spec.header("Mcp-Session-Id", sessionId);
            }

            spec.retrieve()
                    .toBodilessEntity()
                    .subscribe(
                            response -> log.debug("MCP notification sent: {}", method),
                            error -> log.warn("MCP notification failed: {} — {}", method, error.getMessage())
                    );
        } catch (Exception e) {
            log.warn("Failed to send MCP notification: {}", method, e);
        }
    }

    private void initializeProtocol() throws Exception {
        Map<String, Object> params = Map.of(
                "protocolVersion", PROTOCOL_VERSION,
                "capabilities", Map.of(),
                "clientInfo", Map.of("name", "vpn-support-bot", "version", "1.0.0")
        );
        JsonNode response = sendJsonRpc("initialize", params);
        log.info("MCP initialize response: {}", response);

        // Send initialized notification
        sendNotification("notifications/initialized", Map.of());
        log.info("MCP protocol initialized (session: {})", sessionId);
    }

    private void loadTools() throws Exception {
        JsonNode response = sendJsonRpc("tools/list", Map.of());
        JsonNode tools = response.get("tools");
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
            return errorResponse("MCP client not initialized");
        }
        try {
            JsonNode response = sendJsonRpc("tools/call", Map.of(
                    "name", toolName,
                    "arguments", arguments
            ));
            return objectMapper.writeValueAsString(response);
        } catch (Exception e) {
            log.error("Failed to call tool: {}", toolName, e);
            adminNotifier.notifyError("MCP tool call failed: " + toolName, e);
            try {
                return objectMapper.writeValueAsString(Map.of("error", e.getMessage() != null ? e.getMessage() : "unknown error"));
            } catch (JsonProcessingException ex) {
                return "{\"error\":\"tool call failed\"}";
            }
        }
    }

    private String errorResponse(String message) {
        try {
            return objectMapper.writeValueAsString(Map.of("error", message));
        } catch (JsonProcessingException e) {
            return "{\"error\":\"unknown error\"}";
        }
    }
}
