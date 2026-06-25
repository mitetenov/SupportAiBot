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
import org.springframework.stereotype.Component;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

@Component
public class StdioMcpClient implements McpClientInterface {

    private static final Logger log = LoggerFactory.getLogger(StdioMcpClient.class);
    private static final String PROTOCOL_VERSION = "2024-11-05";
    private static final long REQUEST_TIMEOUT_MS = 30_000;

    private final RemnawaveMcpProperties properties;
    private final ObjectMapper objectMapper;
    private final AdminNotifier adminNotifier;

    private Process process;
    private BufferedWriter stdin;
    private BufferedReader stdout;
    private Thread readerThread;
    private final Map<Integer, CompletableFuture<JsonNode>> pendingRequests = new ConcurrentHashMap<>();
    private final AtomicInteger requestId = new AtomicInteger(0);
    private volatile boolean running = false;
    private volatile boolean initialized = false;

    private List<McpTool> cachedTools = Collections.emptyList();

    public StdioMcpClient(RemnawaveMcpProperties properties, ObjectMapper objectMapper,
                          AdminNotifier adminNotifier) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.adminNotifier = adminNotifier;
    }

    @PostConstruct
    public void init() {
        try {
            startProcess();
            initializeProtocol();
            loadTools();
            initialized = true;
            log.info("MCP client initialized with {} tools", cachedTools.size());
        } catch (Exception e) {
            log.error("Failed to initialize MCP client — bot will run without Remnawave tools", e);
            adminNotifier.notifyError("MCP init failed", e);
        }
    }

    @PreDestroy
    public void shutdown() {
        running = false;
        initialized = false;
        if (readerThread != null) {
            readerThread.interrupt();
        }
        if (process != null && process.isAlive()) {
            process.destroy();
            try {
                process.waitFor(5, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                process.destroyForcibly();
            }
        }
    }

    private void startProcess() throws IOException {
        ProcessBuilder pb = new ProcessBuilder(
                properties.getCommand(),
                properties.getScriptPath()
        );
        Map<String, String> env = pb.environment();
        env.put("REMNAWAVE_BASE_URL", properties.getBaseUrl());
        env.put("REMNAWAVE_API_TOKEN", properties.getApiToken());
        env.put("REMNAWAVE_READONLY", String.valueOf(properties.isReadonly()));

        pb.redirectErrorStream(false);
        process = pb.start();

        stdin = new BufferedWriter(new OutputStreamWriter(process.getOutputStream(), StandardCharsets.UTF_8));
        stdout = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));

        BufferedReader stderr = new BufferedReader(new InputStreamReader(process.getErrorStream(), StandardCharsets.UTF_8));
        Thread stderrReader = new Thread(() -> {
            try {
                String line;
                while ((line = stderr.readLine()) != null) {
                    log.warn("MCP stderr: {}", line);
                }
            } catch (IOException e) {
                log.trace("MCP stderr reader ended", e);
            }
        }, "mcp-stderr");
        stderrReader.setDaemon(true);
        stderrReader.start();

        readerThread = new Thread(this::readResponses, "mcp-reader");
        readerThread.setDaemon(true);
        running = true;
        readerThread.start();

        log.info("MCP process started");
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

    @Override
    public List<McpTool> listTools() {
        return cachedTools;
    }

    @Override
    public String callTool(String toolName, Map<String, Object> arguments) {
        if (!initialized) {
            return "{\"error\": \"MCP client not initialized\"}";
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
            return "{\"error\": \"" + e.getMessage() + "\"}";
        }
    }

    private JsonNode sendRequest(String method, Map<String, Object> params) throws Exception {
        int id = requestId.incrementAndGet();
        CompletableFuture<JsonNode> future = new CompletableFuture<>();
        pendingRequests.put(id, future);

        try {
            Map<String, Object> request = new LinkedHashMap<>();
            request.put("jsonrpc", "2.0");
            request.put("id", id);
            request.put("method", method);
            request.put("params", params != null ? params : Map.of());

            String json = objectMapper.writeValueAsString(request);
            synchronized (stdin) {
                stdin.write(json);
                stdin.newLine();
                stdin.flush();
            }
            log.debug("MCP request [{}]: {} {}", id, method, params.keySet());

            return future.get(REQUEST_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } finally {
            pendingRequests.remove(id);
        }
    }

    private void sendNotification(String method, Map<String, Object> params) {
        try {
            Map<String, Object> notification = new LinkedHashMap<>();
            notification.put("jsonrpc", "2.0");
            notification.put("method", method);
            notification.put("params", params);

            String json = objectMapper.writeValueAsString(notification);
            synchronized (stdin) {
                stdin.write(json);
                stdin.newLine();
                stdin.flush();
            }
        } catch (Exception e) {
            log.error("Failed to send notification: {}", method, e);
        }
    }

    private void readResponses() {
        try {
            String line;
            while (running && (line = stdout.readLine()) != null) {
                try {
                    JsonNode message = objectMapper.readTree(line);
                    if (message.has("id") && message.has("result")) {
                        int id = message.get("id").asInt();
                        CompletableFuture<JsonNode> future = pendingRequests.get(id);
                        if (future != null) {
                            if (message.has("error")) {
                                future.completeExceptionally(
                                        new RuntimeException(message.get("error").toString()));
                            } else {
                                future.complete(message.get("result"));
                            }
                        }
                    } else if (message.has("id") && message.has("error")) {
                        int id = message.get("id").asInt();
                        CompletableFuture<JsonNode> future = pendingRequests.get(id);
                        if (future != null) {
                            future.completeExceptionally(
                                    new RuntimeException(message.get("error").toString()));
                        }
                    }
                } catch (JsonProcessingException e) {
                    log.debug("Could not parse MCP message: {}", line);
                }
            }
        } catch (IOException e) {
            if (running) {
                log.error("MCP reader error", e);
                adminNotifier.notifyError("MCP reader crashed", e);
            }
        }
    }
}
