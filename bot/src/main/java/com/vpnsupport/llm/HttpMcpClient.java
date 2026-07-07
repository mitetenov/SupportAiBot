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
import org.springframework.http.HttpStatusCode;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

@Component
public class HttpMcpClient implements McpClientInterface {

    private static final Logger log = LoggerFactory.getLogger(HttpMcpClient.class);
    private static final String PROTOCOL_VERSION = "2024-11-05";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);

    private final RemnawaveMcpProperties properties;
    private final ObjectMapper objectMapper;
    private final AdminNotifier adminNotifier;
    private final WebClient webClient;
    private final AtomicInteger requestId = new AtomicInteger(0);

    private volatile boolean initialized = false;
    private volatile List<McpTool> cachedTools = Collections.emptyList();

    public HttpMcpClient(RemnawaveMcpProperties properties, ObjectMapper objectMapper,
                         AdminNotifier adminNotifier) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.adminNotifier = adminNotifier;
        this.webClient = WebClient.builder()
                .baseUrl(properties.getUrl())
                .defaultHeader("Content-Type", "application/json")
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(
                        HttpClient.create().responseTimeout(REQUEST_TIMEOUT)))
                .build();
    }

    @PostConstruct
    public void init() {
        try {
            initializeProtocol();
            loadTools();
            initialized = true;
            log.info("MCP HTTP client initialized with {} tools at {}", cachedTools.size(), properties.getUrl());
        } catch (Exception e) {
            log.error("Failed to initialize MCP HTTP client — bot will run without Remnawave tools", e);
            adminNotifier.notifyError("MCP HTTP init failed", e);
        }
    }

    @PreDestroy
    public void shutdown() {
        initialized = false;
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
            JsonNode response = sendRequest("tools/call", Map.of(
                    "name", toolName,
                    "arguments", arguments
            ));
            return objectMapper.writeValueAsString(response);
        } catch (Exception e) {
            log.error("Failed to call tool: {}", toolName, e);
            adminNotifier.notifyError("MCP tool call failed: " + toolName, e);
            return errorResponse(e.getMessage() != null ? e.getMessage() : "unknown error");
        }
    }

    private String errorResponse(String message) {
        try {
            return objectMapper.writeValueAsString(Map.of("error", message));
        } catch (JsonProcessingException e) {
            return "{\"error\":\"unknown error\"}";
        }
    }

    private void initializeProtocol() throws Exception {
        Map<String, Object> params = Map.of(
                "protocolVersion", PROTOCOL_VERSION,
                "capabilities", Map.of(),
                "clientInfo", Map.of("name", "vpn-support-bot", "version", "1.0.0")
        );
        JsonNode response = sendRequest("initialize", params);
        log.info("MCP initialize response: {}", response);

        sendNotification("notifications/initialized", Map.of());
        log.info("MCP protocol initialized");
    }

    private void loadTools() throws Exception {
        JsonNode response = sendRequest("tools/list", Map.of());
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

    private JsonNode sendRequest(String method, Map<String, Object> params) throws Exception {
        int id = requestId.updateAndGet(i -> (i + 1) & 0x7FFFFFFF);

        Map<String, Object> request = new LinkedHashMap<>();
        request.put("jsonrpc", "2.0");
        request.put("id", id);
        request.put("method", method);
        request.put("params", params != null ? params : Map.of());

        log.debug("MCP request [{}]: {}", id, method);

        String responseBody = webClient.post()
                .bodyValue(request)
                .retrieve()
                .onStatus(HttpStatusCode::isError,
                        clientResponse -> clientResponse.bodyToMono(String.class)
                                .flatMap(err -> Mono.error(new RuntimeException(
                                        "MCP HTTP error: " + clientResponse.statusCode() + " - " + err))))
                .bodyToMono(String.class)
                .block(REQUEST_TIMEOUT);

        if (responseBody == null) {
            throw new RuntimeException("Empty response from MCP server");
        }

        JsonNode message = objectMapper.readTree(responseBody);
        if (message.has("error")) {
            throw new RuntimeException("MCP error: " + message.get("error"));
        }
        return message.get("result");
    }

    private void sendNotification(String method, Map<String, Object> params) {
        try {
            Map<String, Object> notification = new LinkedHashMap<>();
            notification.put("jsonrpc", "2.0");
            notification.put("method", method);
            notification.put("params", params);

            webClient.post()
                    .bodyValue(notification)
                    .retrieve()
                    .toBodilessEntity()
                    .block(REQUEST_TIMEOUT);
        } catch (Exception e) {
            log.warn("Failed to send MCP notification: {}", method, e.getMessage());
        }
    }
}
