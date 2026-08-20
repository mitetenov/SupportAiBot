package com.vpnsupport.llm;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.RemnawaveMcpProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Gatekeeper between the LLM and the Remnawave MCP servers.
 *
 * <p>Two things are enforced here rather than in the system prompt, because a
 * prompt is advisory and a prompt injection can talk its way around it:
 * <ul>
 *   <li>only allow-listed tools are exposed and callable, with mutating tools
 *       gated behind {@code remnawave.mcp.readonly=false};</li>
 *   <li>the Telegram ID argument is always overwritten with the ID of the user
 *       who actually sent the message, so no conversation can make the bot read
 *       somebody else's account.</li>
 * </ul>
 */
@Component
public class McpRouter {

    private static final Logger log = LoggerFactory.getLogger(McpRouter.class);

    /** Read-only Remnawave tools. Always available. */
    private static final Set<String> READ_TOOLS = Set.of(
            "users_get_by_telegram_id",
            "nodes_list",
            "nodes_get",
            "hwid_devices_list"
    );

    /** Mutating tools. Exposed only when {@code remnawave.mcp.readonly=false}. */
    private static final Set<String> WRITE_TOOLS = Set.of(
            "hwid_device_delete"
    );

    private final List<McpClientInterface> clients;
    private final Map<String, McpClientInterface> toolToClient;
    private final Map<String, TelegramIdParam> telegramIdParamByTool;
    private final Set<String> allowedTools;
    private final ObjectMapper objectMapper;

    public McpRouter(List<McpClientInterface> clients, ObjectMapper objectMapper,
                     RemnawaveMcpProperties properties) {
        this.clients = clients != null ? clients : List.of();
        this.objectMapper = objectMapper;
        this.allowedTools = properties.isReadonly()
                ? READ_TOOLS
                : Stream.concat(READ_TOOLS.stream(), WRITE_TOOLS.stream())
                        .collect(Collectors.toUnmodifiableSet());
        this.toolToClient = buildToolToClientMap();
        this.telegramIdParamByTool = buildTelegramIdParamMap();

        if (properties.isReadonly()) {
            log.info("McpRouter in read-only mode: {} write tool(s) withheld from the model", WRITE_TOOLS.size());
        }
        log.info("McpRouter initialized with {} client(s), {} tool(s) exposed",
                this.clients.size(), listTools().size());
    }

    private Map<String, McpClientInterface> buildToolToClientMap() {
        Map<String, McpClientInterface> map = new LinkedHashMap<>();
        for (McpClientInterface client : clients) {
            for (McpTool tool : client.listTools()) {
                if (allowedTools.contains(tool.name())) {
                    map.putIfAbsent(tool.name(), client);
                }
            }
        }
        return Map.copyOf(map);
    }

    /**
     * Finds, per tool, the input-schema property that carries the Telegram user
     * ID. Deriving the name from the schema means we still pin the argument if
     * the MCP server renames it, and lets us supply it when the model omits it.
     */
    private Map<String, TelegramIdParam> buildTelegramIdParamMap() {
        Map<String, TelegramIdParam> map = new LinkedHashMap<>();
        for (McpTool tool : listTools()) {
            telegramIdProperty(tool.inputSchema())
                    .ifPresent(param -> map.putIfAbsent(tool.name(), param));
        }
        return Map.copyOf(map);
    }

    /**
     * Locates the Telegram ID property and the JSON type the server declares for
     * it. The type matters because the server validates it: {@code mcp-remnawave}
     * declared {@code telegramId} as a string before 3.2.0 and as a number since,
     * and the wrong shape is rejected outright with
     * {@code -32602 Input validation error}. Reading the type off the schema
     * means a future flip needs no change here.
     */
    @SuppressWarnings("unchecked")
    private static Optional<TelegramIdParam> telegramIdProperty(Map<String, Object> inputSchema) {
        if (inputSchema == null || !(inputSchema.get("properties") instanceof Map<?, ?> raw)) {
            return Optional.empty();
        }
        Map<String, Object> properties = (Map<String, Object>) raw;
        return properties.entrySet().stream()
                .filter(e -> isTelegramIdArg(e.getKey()))
                .findFirst()
                .map(e -> new TelegramIdParam(e.getKey(), declaredType(e.getValue())));
    }

    private static String declaredType(Object propertySchema) {
        if (propertySchema instanceof Map<?, ?> schema && schema.get("type") instanceof String type) {
            return type;
        }
        return null;
    }

    /**
     * @param name the argument name
     * @param jsonType the declared JSON type, or null when the schema omits it
     */
    private record TelegramIdParam(String name, String jsonType) {
    }

    public List<McpTool> listTools() {
        List<McpTool> allTools = new ArrayList<>();
        for (McpClientInterface client : clients) {
            for (McpTool tool : client.listTools()) {
                if (allowedTools.contains(tool.name())) {
                    allTools.add(tool);
                }
            }
        }
        return allTools;
    }

    /**
     * Executes a tool on behalf of {@code telegramUserId}. The caller's ID wins
     * over anything the model put in {@code arguments}.
     */
    public String callTool(String toolName, Map<String, Object> arguments, long telegramUserId) {
        if (!allowedTools.contains(toolName)) {
            log.warn("Blocked call to non-allowed tool: {}", toolName);
            return errorJson("Tool not allowed: " + toolName);
        }

        McpClientInterface client = toolToClient.get(toolName);
        if (client == null) {
            log.warn("Unknown tool requested: {}", toolName);
            return errorJson("Unknown tool: " + toolName);
        }

        return client.callTool(toolName, pinTelegramId(toolName, arguments, telegramUserId));
    }

    /**
     * Overwrites (or supplies) the Telegram ID argument with the real sender's
     * ID. An LLM that was talked into reading another account still ends up
     * querying the caller's own record.
     */
    private Map<String, Object> pinTelegramId(String toolName, Map<String, Object> arguments,
                                              long telegramUserId) {
        Map<String, Object> safe = new LinkedHashMap<>();
        if (arguments != null) {
            safe.putAll(arguments);
        }

        TelegramIdParam schemaParam = telegramIdParamByTool.get(toolName);

        safe.keySet().stream()
                .filter(McpRouter::isTelegramIdArg)
                .toList()
                .forEach(key -> {
                    Object supplied = safe.get(key);
                    if (!matchesUser(supplied, telegramUserId)) {
                        log.warn("Tool {} called with {}={} — overriding with the actual sender {}",
                                toolName, key, supplied, telegramUserId);
                    }
                    safe.put(key, coerce(telegramUserId, schemaParam, supplied));
                });

        if (schemaParam != null && !safe.containsKey(schemaParam.name())) {
            safe.put(schemaParam.name(), coerce(telegramUserId, schemaParam, null));
        }
        return safe;
    }

    /**
     * Renders the ID as the type the tool expects.
     *
     * <p>The schema wins when it declares one. Otherwise the shape the model
     * chose is kept — it read the same schema — and a string is the last resort,
     * since IDs beyond 2^53 are unsafe as JSON numbers anyway.
     */
    private static Object coerce(long telegramUserId, TelegramIdParam param, Object supplied) {
        String jsonType = param != null ? param.jsonType() : null;
        if (jsonType != null) {
            return switch (jsonType) {
                case "string" -> String.valueOf(telegramUserId);
                case "number", "integer" -> telegramUserId;
                default -> String.valueOf(telegramUserId);
            };
        }
        if (supplied instanceof Number) {
            return telegramUserId;
        }
        return String.valueOf(telegramUserId);
    }

    private static boolean isTelegramIdArg(String key) {
        return key != null && key.replace("_", "").equalsIgnoreCase("telegramid");
    }

    private static boolean matchesUser(Object supplied, long telegramUserId) {
        if (supplied instanceof Number number) {
            return number.longValue() == telegramUserId;
        }
        return supplied != null && String.valueOf(telegramUserId).equals(String.valueOf(supplied).trim());
    }

    private String errorJson(String message) {
        try {
            return objectMapper.writeValueAsString(Map.of("error", message));
        } catch (JsonProcessingException e) {
            return "{\"error\":\"Unknown tool\"}";
        }
    }
}
