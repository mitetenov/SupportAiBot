package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.config.GeminiProperties;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@Component
@ConditionalOnProperty(name = "llm.provider", havingValue = "gemini")
public class GeminiClient implements LlmClient {

    private static final Logger log = LoggerFactory.getLogger(GeminiClient.class);
    private static final int MAX_TOOL_ITERATIONS = 5;

    private final WebClient webClient;
    private final ObjectMapper objectMapper;
    private final McpClient mcpClient;
    private final ChatHistoryService chatHistoryService;
    private final FaqEmbeddingService faqEmbeddingService;
    private final String model;

    public GeminiClient(GeminiProperties properties, ObjectMapper objectMapper,
                        McpClient mcpClient, ChatHistoryService chatHistoryService,
                        FaqEmbeddingService faqEmbeddingService) {
        this.objectMapper = objectMapper;
        this.mcpClient = mcpClient;
        this.chatHistoryService = chatHistoryService;
        this.faqEmbeddingService = faqEmbeddingService;
        this.model = properties.getModel();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Content-Type", "application/json")
                .defaultHeader("x-goog-api-key", properties.getApiKey())
                .build();
    }

    @Override
    public boolean supportsImages() {
        return true;
    }

    @Override
    public String chat(String userMessage, long telegramUserId) {
        String response = chatInternal(userMessage, telegramUserId, null, null, 0);
        if (!isErrorResponse(response)) {
            chatHistoryService.addUserMessage(telegramUserId, userMessage);
            chatHistoryService.addAssistantMessage(telegramUserId, response);
        }
        return response;
    }

    @Override
    public String chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        String response = chatInternal(userMessage, telegramUserId, base64Image, mimeType, 0);
        if (!isErrorResponse(response)) {
            String historyMessage = userMessage.isBlank() ? "[Скриншот]" : userMessage;
            chatHistoryService.addUserMessage(telegramUserId, historyMessage);
            chatHistoryService.addAssistantMessage(telegramUserId, response);
        }
        return response;
    }

    private String chatInternal(String userMessage, long telegramUserId,
                                 String base64Image, String mimeType, int iteration) {
        if (iteration >= MAX_TOOL_ITERATIONS) {
            return "Превышено количество попыток обработки запроса. Пожалуйста, попробуйте ещё раз.";
        }

        try {
            String faqContext = faqEmbeddingService.buildFaqContext(userMessage);
            ObjectNode requestBody = buildRequestBody(userMessage, telegramUserId,
                    base64Image, mimeType, faqContext);

            log.debug("Gemini request (iteration {}): {} tools available",
                    iteration, mcpClient.listTools().size());

            String response = executeGenerateContent(requestBody);

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode candidates = jsonResponse.get("candidates");
            if (candidates == null || !candidates.isArray() || candidates.isEmpty()) {
                String blockReason = jsonResponse.has("promptFeedback")
                        ? jsonResponse.get("promptFeedback").toString()
                        : "неизвестно";
                log.error("Empty candidates in Gemini response. Block: {}", blockReason);
                return "Не удалось получить ответ от модели. Возможно, запрос был заблокирован фильтрами.";
            }

            JsonNode content = candidates.get(0).get("content");
            JsonNode parts = content.get("parts");

            if (parts == null || !parts.isArray()) {
                return "Модель не вернула ответа. Попробуйте переформулировать вопрос.";
            }

            StringBuilder textResponse = new StringBuilder();
            List<JsonNode> pendingFunctionCalls = new ArrayList<>();

            for (JsonNode part : parts) {
                if (part.has("text") && !part.get("text").isNull()) {
                    textResponse.append(part.get("text").asText());
                }
                if (part.has("functionCall")) {
                    pendingFunctionCalls.add(part.get("functionCall"));
                }
            }

            if (!pendingFunctionCalls.isEmpty()) {
                log.info("Gemini requested {} tool call(s)", pendingFunctionCalls.size());

                List<Map<String, Object>> contents = buildContentsList(userMessage,
                        telegramUserId, base64Image, mimeType);

                ObjectNode modelContent = objectMapper.createObjectNode();
                modelContent.put("role", "model");
                ArrayNode modelParts = modelContent.putArray("parts");
                for (JsonNode part : parts) {
                    modelParts.add(part);
                }
                contents.add(objectMapper.convertValue(modelContent, Map.class));

                for (JsonNode functionCall : pendingFunctionCalls) {
                    String functionName = functionCall.get("name").asText();
                    JsonNode argsNode = functionCall.get("args");

                    @SuppressWarnings("unchecked")
                    Map<String, Object> arguments = argsNode != null && !argsNode.isNull()
                            ? objectMapper.convertValue(argsNode, Map.class)
                            : Map.of();

                    log.info("Executing tool: {} with args: {}", functionName, arguments);

                    String toolResult = mcpClient.callTool(functionName, arguments);
                    log.info("Tool {} result: {}",
                            functionName,
                            toolResult.length() > 300 ? toolResult.substring(0, 300) + "..." : toolResult);

                    Map<String, Object> functionResponse = Map.of(
                            "role", "function",
                            "parts", List.of(Map.of(
                                    "functionResponse", Map.of(
                                            "name", functionName,
                                            "response", parseToolResultForGemini(toolResult)
                                    )
                            ))
                    );
                    contents.add(functionResponse);
                }

                if (iteration == 0) {
                    List<String> toolResults = contents.stream()
                            .filter(m -> "function".equals(m.get("role")))
                            .map(m -> {
                                @SuppressWarnings("unchecked")
                                var funcParts = (List<Map<String, Object>>) m.get("parts");
                                @SuppressWarnings("unchecked")
                                var fr = (Map<String, Object>) funcParts.get(0).get("functionResponse");
                                return String.valueOf(fr.get("response"));
                            })
                            .toList();
                    String refreshedFaq = faqEmbeddingService.buildRefinedFaqContext(
                            userMessage, toolResults);
                    if (!refreshedFaq.isEmpty()) {
                        contents.add(Map.of("role", "user", "parts", List.of(
                                Map.of("text",
                                        "[Система] Результат диагностики получен. Актуальный FAQ:\n\n"
                                                + refreshedFaq))));
                    }
                }

                return continueChat(contents, iteration + 1);
            }

            if (!textResponse.isEmpty()) {
                return textResponse.toString();
            }

            return "Модель не вернула текстового ответа. Попробуйте переформулировать вопрос.";

        } catch (Exception e) {
            log.error("Gemini request failed", e);
            return "Произошла ошибка при обработке запроса. Попробуйте позже.";
        }
    }

    private String continueChat(List<Map<String, Object>> contents, int iteration) {
        if (iteration >= MAX_TOOL_ITERATIONS) {
            return "Превышено количество попыток обработки запроса.";
        }

        try {
            ObjectNode requestBody = objectMapper.createObjectNode();

            ObjectNode systemInstruction = objectMapper.createObjectNode();
            ArrayNode systemParts = systemInstruction.putArray("parts");
            systemParts.addObject().put("text", SupportPrompt.SYSTEM);
            requestBody.set("system_instruction", systemInstruction);

            ArrayNode contentsArray = requestBody.putArray("contents");
            for (Map<String, Object> msg : contents) {
                contentsArray.add(objectMapper.valueToTree(msg));
            }

            addTools(requestBody);

            String response = executeGenerateContent(requestBody);

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode candidates = jsonResponse.get("candidates");
            if (candidates == null || !candidates.isArray() || candidates.isEmpty()) {
                return "Не удалось получить ответ.";
            }

            JsonNode parts = candidates.get(0).get("content").get("parts");
            if (parts == null || !parts.isArray()) {
                return "Пустой ответ модели.";
            }

            StringBuilder textResponse = new StringBuilder();
            List<JsonNode> pendingFunctionCalls = new ArrayList<>();

            for (JsonNode part : parts) {
                if (part.has("text") && !part.get("text").isNull()) {
                    textResponse.append(part.get("text").asText());
                }
                if (part.has("functionCall")) {
                    pendingFunctionCalls.add(part.get("functionCall"));
                }
            }

            if (!pendingFunctionCalls.isEmpty()) {
                ObjectNode modelContent = objectMapper.createObjectNode();
                modelContent.put("role", "model");
                ArrayNode modelParts = modelContent.putArray("parts");
                for (JsonNode part : parts) {
                    modelParts.add(part);
                }
                contents.add(objectMapper.convertValue(modelContent, Map.class));

                for (JsonNode functionCall : pendingFunctionCalls) {
                    String functionName = functionCall.get("name").asText();
                    JsonNode argsNode = functionCall.get("args");

                    @SuppressWarnings("unchecked")
                    Map<String, Object> arguments = argsNode != null && !argsNode.isNull()
                            ? objectMapper.convertValue(argsNode, Map.class)
                            : Map.of();

                    log.info("Executing tool: {} with args: {}", functionName, arguments);

                    String toolResult = mcpClient.callTool(functionName, arguments);
                    log.info("Tool {} result: {}",
                            functionName,
                            toolResult.length() > 300 ? toolResult.substring(0, 300) + "..." : toolResult);

                    Map<String, Object> functionResponse = Map.of(
                            "role", "function",
                            "parts", List.of(Map.of(
                                    "functionResponse", Map.of(
                                            "name", functionName,
                                            "response", parseToolResultForGemini(toolResult)
                                    )
                            ))
                    );
                    contents.add(functionResponse);
                }

                return continueChat(contents, iteration + 1);
            }

            if (!textResponse.isEmpty()) {
                return textResponse.toString();
            }

            return "Модель не вернула текстового ответа.";

        } catch (Exception e) {
            log.error("Gemini continue chat failed", e);
            return "Ошибка при обработке. Попробуйте позже.";
        }
    }

    private String executeGenerateContent(ObjectNode requestBody) {
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

    private ObjectNode buildRequestBody(String userMessage, long telegramUserId,
                                         String base64Image, String mimeType,
                                         String faqContext) {
        ObjectNode body = objectMapper.createObjectNode();

        ObjectNode systemInstruction = objectMapper.createObjectNode();
        ArrayNode systemParts = systemInstruction.putArray("parts");
        systemParts.addObject().put("text",
                SupportPrompt.withFaqContext(faqContext, telegramUserId));
        body.set("system_instruction", systemInstruction);

        ArrayNode contentsArray = body.putArray("contents");
        for (Map<String, Object> historyMessage : chatHistoryService.toGeminiContents(telegramUserId)) {
            contentsArray.add(objectMapper.valueToTree(historyMessage));
        }

        ObjectNode userContent = objectMapper.createObjectNode();
        userContent.put("role", "user");
        ArrayNode userParts = userContent.putArray("parts");

        if (base64Image != null && !base64Image.isEmpty()) {
            userParts.addObject().put("text", userMessage);
            ObjectNode imagePart = userParts.addObject();
            ObjectNode inlineData = imagePart.putObject("inline_data");
            inlineData.put("mime_type", mimeType != null ? mimeType : "image/jpeg");
            inlineData.put("data", base64Image);
        } else {
            userParts.addObject().put("text", userMessage);
        }

        contentsArray.add(userContent);

        addTools(body);

        ObjectNode toolConfig = body.putObject("tool_config");
        ObjectNode functionCallingConfig = toolConfig.putObject("function_calling_config");
        functionCallingConfig.put("mode", "AUTO");

        return body;
    }

    private List<Map<String, Object>> buildContentsList(String userMessage, long telegramUserId,
                                                          String base64Image, String mimeType) {
        List<Map<String, Object>> contents = new ArrayList<>(chatHistoryService.toGeminiContents(telegramUserId));

        Map<String, Object> userContent = new java.util.LinkedHashMap<>();
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

    private void addTools(ObjectNode body) {
        List<McpTool> tools = mcpClient.listTools();
        if (tools.isEmpty()) {
            return;
        }

        ArrayNode toolsArray = body.putArray("tools");
        ObjectNode toolsEntry = toolsArray.addObject();
        ArrayNode functionDeclarations = toolsEntry.putArray("function_declarations");

        for (McpTool tool : tools) {
            ObjectNode functionDeclaration = functionDeclarations.addObject();
            functionDeclaration.put("name", tool.getName());
            if (tool.getDescription() != null) {
                functionDeclaration.put("description", tool.getDescription());
            }
            if (tool.getInputSchema() != null && !tool.getInputSchema().isEmpty()) {
                JsonNode paramsNode = objectMapper.valueToTree(tool.getInputSchema());
                ObjectNode sanitized = sanitizeSchemaParams(paramsNode);
                functionDeclaration.set("parameters", sanitized);
            }
        }
    }

    private ObjectNode sanitizeSchemaParams(JsonNode schema) {
        ObjectNode cleaned = objectMapper.createObjectNode();
        schema.fieldNames().forEachRemaining(field -> {
            if ("$schema".equals(field) || "additionalProperties".equals(field)) {
                return;
            }
            JsonNode value = schema.get(field);
            if ("properties".equals(field) && value != null && value.isObject()) {
                ObjectNode cleanedProps = objectMapper.createObjectNode();
                value.fieldNames().forEachRemaining(propName -> {
                    cleanedProps.set(propName, sanitizeSchemaParams(value.get(propName)));
                });
                cleaned.set("properties", cleanedProps);
            } else if (("items".equals(field) || "additionalProperties_replacement".equals(field))
                    && value != null && value.isObject()) {
                cleaned.set(field, sanitizeSchemaParams(value));
            } else {
                cleaned.set(field, value);
            }
        });
        return cleaned;
    }

    @SuppressWarnings("unchecked")
    private Object parseToolResultForGemini(String toolResult) {
        try {
            return objectMapper.readValue(toolResult, Map.class);
        } catch (Exception e) {
            return Map.of("output", toolResult);
        }
    }

    private boolean isErrorResponse(String response) {
        return response.startsWith("Превышено количество")
                || response.startsWith("Не удалось")
                || response.startsWith("Произошла ошибка")
                || response.startsWith("Модель не вернула")
                || response.startsWith("Пустой ответ")
                || response.startsWith("Ошибка при обработке");
    }
}
