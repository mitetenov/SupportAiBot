package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsage;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.GeminiProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

@Component
@ConditionalOnProperty(name = "llm.provider", havingValue = "gemini")
public class GeminiClient extends AbstractLlmClient {

    private static final String ROLE_KEY = "role";
    private static final String PARTS_KEY = "parts";
    private static final String FUNCTION_CALL_KEY = "functionCall";
    private static final String THOUGHT_SIGNATURE_KEY = "thought_signature";


    private final WebClient webClient;
    private final String model;
    /**
     * Built once at construction: {@link McpRouter} already has the tool list by
     * the time this bean is created, so the lazy double-checked locking this
     * replaced never had anything to defer.
     */
    private final List<JsonNode> sanitizedTools;

    public GeminiClient(GeminiProperties properties, ObjectMapper objectMapper,
                        McpRouter mcpRouter, ChatHistoryService chatHistoryService,
                        FaqEmbeddingService faqEmbeddingService,
                        LlmTokenUsageRepository tokenUsageRepository) {
        super(objectMapper, mcpRouter, chatHistoryService, faqEmbeddingService, tokenUsageRepository);
        this.model = properties.getModel();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Content-Type", "application/json")
                .defaultHeader("x-goog-api-key", properties.getApiKey())
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(
                        HttpClient.create().responseTimeout(Duration.ofSeconds(60))))
                .build();
        this.sanitizedTools = buildSanitizedTools();
    }

    @Override
    public boolean supportsImages() {
        return true;
    }

    @Override
    @SuppressWarnings("unused")
    protected List<Map<String, Object>> buildInitialConversation(
            String userMessage, long telegramUserId, String faqContext,
            String base64Image, String mimeType) {
        List<Map<String, Object>> contents = new ArrayList<>();

        String dynamicContext = "Telegram ID: " + telegramUserId;
        if (faqContext != null && !faqContext.isEmpty()) {
            dynamicContext += "\n\n" + faqContext;
        }
        contents.add(Map.of(
                ROLE_KEY, "user",
                PARTS_KEY, List.of(Map.of("text", "[Система: Контекст текущего пользователя]\n" + dynamicContext))
        ));
        contents.add(Map.of(
                ROLE_KEY, "model",
                PARTS_KEY, List.of(Map.of("text", "Принято. Я готов помочь пользователю."))
        ));

        contents.addAll(chatHistoryService.toGeminiContents(telegramUserId));

        Map<String, Object> userContent = new LinkedHashMap<>();
        userContent.put(ROLE_KEY, "user");
        List<Object> userParts = new ArrayList<>();

        if (base64Image != null && !base64Image.isEmpty()) {
            userParts.add(Map.of("text", userMessage));
            userParts.add(Map.of("inline_data", Map.of(
                    "mime_type", mimeType != null ? mimeType : "image/jpeg",
                    "data", base64Image
            )));
        } else {
            userParts.add(Map.of("text", userMessage));
        }
        userContent.put(PARTS_KEY, userParts);
        contents.add(userContent);

        return contents;
    }

    @Override
    protected String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId) {
        ObjectNode requestBody = buildRequestBody(conversation);
        log.debug("Gemini request ({} tools available)", sanitizedTools.size());

        return webClient.post()
                .uri("/models/{model}:generateContent", model)
                .bodyValue(requestBody)
                .retrieve()
                .onStatus(status -> status.is4xxClientError() || status.is5xxServerError(),
                        clientResponse -> clientResponse.bodyToMono(String.class)
                                .flatMap(err -> Mono.error(new RuntimeException(
                                        "Gemini API error: " + clientResponse.statusCode() + " - " + err))))
                .bodyToMono(String.class)
                .block();
    }

    @Override
    protected LlmResponse parseResponse(String rawResponse) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode candidates = jsonResponse.get("candidates");
            if (candidates == null || !candidates.isArray() || candidates.isEmpty()) {
                String blockReason = jsonResponse.has("promptFeedback")
                        ? jsonResponse.get("promptFeedback").toString()
                        : "неизвестно";
                log.error("Empty candidates in Gemini response. Block: {}", blockReason);
                throw new LlmProcessingException("Empty candidates",
                        "Не удалось получить ответ от модели. Возможно, запрос был заблокирован фильтрами.");
            }

            JsonNode content = candidates.get(0).get("content");
            if (content == null) {
                throw new LlmProcessingException("No content in candidate",
                        "Модель не вернула ответа. Попробуйте переформулировать вопрос.");
            }

            JsonNode parts = content.get(PARTS_KEY);
            if (parts == null || !parts.isArray()) {
                throw new LlmProcessingException("Empty parts",
                        "Модель не вернула ответа. Попробуйте переформулировать вопрос.");
            }

            StringBuilder textResponse = new StringBuilder();
            List<LlmResponse.ToolCall> functionCalls = new ArrayList<>();
            List<Map<String, Object>> rawParts = new ArrayList<>();

            for (JsonNode part : parts) {
                @SuppressWarnings("unchecked")
                Map<String, Object> rawPart = objectMapper.convertValue(part, Map.class);
                rawParts.add(rawPart);

                if (part.has("text") && !part.get("text").isNull()) {
                    textResponse.append(part.get("text").asText());
                }
                if (part.has(FUNCTION_CALL_KEY)) {
                    functionCalls.add(parseFunctionCall(part.get(FUNCTION_CALL_KEY)));
                }
            }

            return new LlmResponse(textResponse.toString(), functionCalls, rawParts);
        } catch (LlmProcessingException e) {
            throw e;
        } catch (Exception e) {
            throw new LlmProcessingException("Parse error",
                    "Ошибка обработки ответа модели.");
        }
    }

    private LlmResponse.ToolCall parseFunctionCall(JsonNode fc) {
        JsonNode argsNode = fc.get("args");
        @SuppressWarnings("unchecked")
        Map<String, Object> arguments = argsNode != null && !argsNode.isNull()
                ? objectMapper.convertValue(argsNode, Map.class)
                : Map.of();
        String thoughtSignature = fc.has(THOUGHT_SIGNATURE_KEY)
                ? fc.get(THOUGHT_SIGNATURE_KEY).asText()
                : null;
        return new LlmResponse.ToolCall(fc.get("name").asText(), "", arguments, thoughtSignature);
    }

    @Override
    protected void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response) {
        Map<String, Object> modelContent = new LinkedHashMap<>();
        modelContent.put(ROLE_KEY, "model");
        List<Map<String, Object>> modelParts = new ArrayList<>();

        if (!response.rawParts().isEmpty()) {
            modelParts.addAll(response.rawParts());
        } else {
            if (response.text() != null && !response.text().isEmpty()) {
                modelParts.add(Map.of("text", response.text()));
            }
            for (LlmResponse.ToolCall tc : response.toolCalls()) {
                Map<String, Object> fc = new LinkedHashMap<>();
                fc.put("name", tc.name());
                fc.put("args", tc.arguments());
                if (tc.thoughtSignature() != null) {
                    fc.put(THOUGHT_SIGNATURE_KEY, tc.thoughtSignature());
                }
                modelParts.add(Map.of(FUNCTION_CALL_KEY, fc));
            }
        }
        modelContent.put(PARTS_KEY, modelParts);
        conversation.add(modelContent);
    }

    @Override
    @SuppressWarnings("unchecked")
    protected void addToolResultToConversation(List<Map<String, Object>> conversation,
                                                LlmResponse.ToolCall toolCall, String toolResult) {
        Map<String, Object> responseContent;
        try {
            responseContent = objectMapper.readValue(toolResult, Map.class);
        } catch (Exception e) {
            responseContent = Map.of("output", toolResult);
        }
        Map<String, Object> functionResponse = new LinkedHashMap<>();
        functionResponse.put("name", toolCall.name());
        functionResponse.put("response", responseContent);
        if (toolCall.thoughtSignature() != null) {
            functionResponse.put(THOUGHT_SIGNATURE_KEY, toolCall.thoughtSignature());
        }
        conversation.add(Map.of(
                ROLE_KEY, "function",
                PARTS_KEY, List.of(Map.of("functionResponse", functionResponse))
        ));
    }



    @Override
    protected void saveUsage(String rawResponse, long telegramUserId) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode usage = jsonResponse.get("usageMetadata");
            if (usage != null) {
                long promptTokens = usage.get("promptTokenCount").asLong();
                long completionTokens = usage.get("candidatesTokenCount").asLong();
                long totalTokens = usage.get("totalTokenCount").asLong();
                tokenUsageRepository.save(new LlmTokenUsage(
                        telegramUserId, promptTokens, completionTokens, totalTokens));
            }
        } catch (Exception e) {
            log.warn("Failed to save token usage: {}", e.getMessage());
        }
    }

    @Override
    protected String getProviderName() {
        return "Gemini";
    }

    private List<JsonNode> buildSanitizedTools() {
        List<McpTool> tools = mcpRouter.listTools();
        if (tools.isEmpty()) {
            return List.of();
        }
        List<JsonNode> sanitized = new ArrayList<>();
        for (McpTool tool : tools) {
            ObjectNode functionDeclaration = objectMapper.createObjectNode();
            functionDeclaration.put("name", tool.name());
            if (tool.description() != null) {
                functionDeclaration.put("description", tool.description());
            }
            if (tool.inputSchema() != null && !tool.inputSchema().isEmpty()) {
                JsonNode paramsNode = objectMapper.valueToTree(tool.inputSchema());
                functionDeclaration.set("parameters", sanitizeSchemaParams(paramsNode));
            }
            sanitized.add(functionDeclaration);
        }
        return List.copyOf(sanitized);
    }

    // Package-private so the test can call it without reflection.
    ObjectNode buildRequestBody(List<Map<String, Object>> contents) {
        ObjectNode body = objectMapper.createObjectNode();

        ObjectNode systemInstruction = objectMapper.createObjectNode();
        ArrayNode systemParts = systemInstruction.putArray(PARTS_KEY);
        systemParts.addObject().put("text", SupportPrompt.SYSTEM);
        body.set("system_instruction", systemInstruction);

        ArrayNode contentsArray = body.putArray("contents");
        for (Map<String, Object> msg : contents) {
            contentsArray.add(objectMapper.valueToTree(msg));
        }

        List<JsonNode> tools = sanitizedTools;
        if (!tools.isEmpty()) {
            ArrayNode toolsArray = body.putArray("tools");
            ObjectNode toolsEntry = toolsArray.addObject();
            ArrayNode functionDeclarations = toolsEntry.putArray("function_declarations");
            for (JsonNode decl : tools) {
                functionDeclarations.add(decl);
            }
        }

        ObjectNode toolConfig = body.putObject("tool_config");
        ObjectNode functionCallingConfig = toolConfig.putObject("function_calling_config");
        functionCallingConfig.put("mode", "AUTO");

        return body;
    }

    /** Fields Gemini's function-declaration schema does not accept. */
    private static final Set<String> UNSUPPORTED_SCHEMA_FIELDS =
            Set.of("$schema", "additionalProperties", "propertyNames");

    /** Fields whose value is an array of nested schemas. */
    private static final Set<String> SCHEMA_ARRAY_FIELDS = Set.of("anyOf", "oneOf", "allOf");

    /**
     * Rewrites a JSON Schema from the MCP server into the subset Gemini accepts
     * in a function declaration.
     */
    private ObjectNode sanitizeSchemaParams(JsonNode schema) {
        ObjectNode cleaned = objectMapper.createObjectNode();
        schema.fieldNames().forEachRemaining(field -> {
            if (!UNSUPPORTED_SCHEMA_FIELDS.contains(field)) {
                copySanitizedField(cleaned, field, schema.get(field));
            }
        });
        return cleaned;
    }

    private void copySanitizedField(ObjectNode cleaned, String field, JsonNode value) {
        switch (field) {
            case "const" -> cleaned.putArray("enum").add(value);
            case "any_of" -> copySchemaArray(cleaned, "anyOf", value);
            case "properties" -> copyProperties(cleaned, field, value);
            case "items" -> copyNestedSchema(cleaned, field, value);
            default -> {
                if (SCHEMA_ARRAY_FIELDS.contains(field) && value.isArray()) {
                    copySchemaArray(cleaned, field, value);
                } else {
                    cleaned.set(field, value);
                }
            }
        }
    }

    private void copySchemaArray(ObjectNode cleaned, String targetField, JsonNode value) {
        ArrayNode target = cleaned.putArray(targetField);
        if (value.isArray()) {
            for (JsonNode item : value) {
                target.add(sanitizeSchemaParams(item));
            }
        }
    }

    private void copyProperties(ObjectNode cleaned, String field, JsonNode value) {
        if (!value.isObject()) {
            cleaned.set(field, value);
            return;
        }
        ObjectNode cleanedProps = objectMapper.createObjectNode();
        value.fieldNames().forEachRemaining(propName ->
                cleanedProps.set(propName, sanitizeSchemaParams(value.get(propName))));
        cleaned.set(field, cleanedProps);
    }

    private void copyNestedSchema(ObjectNode cleaned, String field, JsonNode value) {
        cleaned.set(field, value.isObject() ? sanitizeSchemaParams(value) : value);
    }
}
