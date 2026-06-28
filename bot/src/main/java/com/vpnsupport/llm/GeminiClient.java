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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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

@Component
@ConditionalOnProperty(name = "llm.provider", havingValue = "gemini")
public class GeminiClient extends AbstractLlmClient {

    private static final Logger log = LoggerFactory.getLogger(GeminiClient.class);

    private final WebClient webClient;
    private final String model;
    private final List<JsonNode> cachedSanitizedTools;

    public GeminiClient(GeminiProperties properties, ObjectMapper objectMapper,
                        McpRouter mcpRouter, ChatHistoryService chatHistoryService,
                        FaqEmbeddingService faqEmbeddingService,
                        LlmTokenUsageRepository tokenUsageRepository) {
        super(objectMapper, mcpRouter, chatHistoryService, faqEmbeddingService, tokenUsageRepository);
        this.model = properties.getModel();
        this.cachedSanitizedTools = buildSanitizedTools();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Content-Type", "application/json")
                .defaultHeader("x-goog-api-key", properties.getApiKey())
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(
                        HttpClient.create().responseTimeout(Duration.ofSeconds(60))))
                .build();
    }

    @Override
    public boolean supportsImages() {
        return true;
    }

    @Override
    protected List<Map<String, Object>> buildInitialConversation(
            String userMessage, long telegramUserId, String faqContext,
            String base64Image, String mimeType) {
        List<Map<String, Object>> contents = new ArrayList<>(chatHistoryService.toGeminiContents(telegramUserId));

        Map<String, Object> userContent = new LinkedHashMap<>();
        userContent.put("role", "user");
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
        userContent.put("parts", userParts);
        contents.add(userContent);

        return contents;
    }

    @Override
    protected String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId) {
        ObjectNode requestBody = buildRequestBody(conversation, faqContext, telegramUserId);
        log.debug("Gemini request ({} tools available)", cachedSanitizedTools.size());

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

            JsonNode parts = content.get("parts");
            if (parts == null || !parts.isArray()) {
                throw new LlmProcessingException("Empty parts",
                        "Модель не вернула ответа. Попробуйте переформулировать вопрос.");
            }

            StringBuilder textResponse = new StringBuilder();
            List<LlmResponse.ToolCall> functionCalls = new ArrayList<>();

            for (JsonNode part : parts) {
                if (part.has("text") && !part.get("text").isNull()) {
                    textResponse.append(part.get("text").asText());
                }
                if (part.has("functionCall")) {
                    JsonNode fc = part.get("functionCall");
                    String fnName = fc.get("name").asText();
                    JsonNode argsNode = fc.get("args");
                    @SuppressWarnings("unchecked")
                    Map<String, Object> arguments = argsNode != null && !argsNode.isNull()
                            ? objectMapper.convertValue(argsNode, Map.class)
                            : Map.of();
                    functionCalls.add(new LlmResponse.ToolCall(fnName, "", arguments));
                }
            }

            return new LlmResponse(textResponse.toString(), functionCalls);
        } catch (LlmProcessingException e) {
            throw e;
        } catch (Exception e) {
            throw new LlmProcessingException("Parse error",
                    "Ошибка обработки ответа модели.");
        }
    }

    @Override
    protected void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response) {
        Map<String, Object> modelContent = new LinkedHashMap<>();
        modelContent.put("role", "model");
        List<Map<String, Object>> modelParts = new ArrayList<>();

        if (response.text() != null && !response.text().isEmpty()) {
            modelParts.add(Map.of("text", response.text()));
        }
        for (LlmResponse.ToolCall tc : response.toolCalls()) {
            modelParts.add(Map.of("functionCall", Map.of(
                    "name", tc.name(),
                    "args", tc.arguments()
            )));
        }
        modelContent.put("parts", modelParts);
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
        conversation.add(Map.of(
                "role", "function",
                "parts", List.of(Map.of(
                        "functionResponse", Map.of(
                                "name", toolCall.name(),
                                "response", responseContent
                        )
                ))
        ));
    }

    @Override
    protected void injectFaqToConversation(List<Map<String, Object>> conversation, String faqText) {
        conversation.add(Map.of("role", "user", "parts", List.of(
                Map.of("text", "[Система] Результат диагностики получен. Актуальный FAQ:\n\n" + faqText))));
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

    private ObjectNode buildRequestBody(List<Map<String, Object>> contents, String faqContext, long telegramUserId) {
        ObjectNode body = objectMapper.createObjectNode();

        ObjectNode systemInstruction = objectMapper.createObjectNode();
        ArrayNode systemParts = systemInstruction.putArray("parts");
        systemParts.addObject().put("text", SupportPrompt.withFaqContext(faqContext, telegramUserId));
        body.set("system_instruction", systemInstruction);

        ArrayNode contentsArray = body.putArray("contents");
        for (Map<String, Object> msg : contents) {
            contentsArray.add(objectMapper.valueToTree(msg));
        }

        if (!cachedSanitizedTools.isEmpty()) {
            ArrayNode toolsArray = body.putArray("tools");
            ObjectNode toolsEntry = toolsArray.addObject();
            ArrayNode functionDeclarations = toolsEntry.putArray("function_declarations");
            for (JsonNode decl : cachedSanitizedTools) {
                functionDeclarations.add(decl);
            }
        }

        ObjectNode toolConfig = body.putObject("tool_config");
        ObjectNode functionCallingConfig = toolConfig.putObject("function_calling_config");
        functionCallingConfig.put("mode", "AUTO");

        return body;
    }

    private ObjectNode sanitizeSchemaParams(JsonNode schema) {
        ObjectNode cleaned = objectMapper.createObjectNode();
        schema.fieldNames().forEachRemaining(field -> {
            if ("$schema".equals(field) || "additionalProperties".equals(field) || "propertyNames".equals(field)) {
                return;
            }
            JsonNode value = schema.get(field);
            if ("const".equals(field)) {
                ArrayNode enumValues = cleaned.putArray("enum");
                enumValues.add(value);
                return;
            }
            if ("any_of".equals(field)) {
                ArrayNode anyOf = cleaned.putArray("anyOf");
                if (value.isArray()) {
                    for (JsonNode item : value) {
                        anyOf.add(sanitizeSchemaParams(item));
                    }
                }
                return;
            }
            if ("properties".equals(field) && value.isObject()) {
                ObjectNode cleanedProps = objectMapper.createObjectNode();
                value.fieldNames().forEachRemaining(propName ->
                        cleanedProps.set(propName, sanitizeSchemaParams(value.get(propName))));
                cleaned.set("properties", cleanedProps);
            } else if ("items".equals(field) && value.isObject()) {
                cleaned.set(field, sanitizeSchemaParams(value));
            } else if (("anyOf".equals(field) || "oneOf".equals(field) || "allOf".equals(field)) && value.isArray()) {
                ArrayNode arr = cleaned.putArray(field);
                for (JsonNode item : value) {
                    arr.add(sanitizeSchemaParams(item));
                }
            } else {
                cleaned.set(field, value);
            }
        });
        return cleaned;
    }
}
