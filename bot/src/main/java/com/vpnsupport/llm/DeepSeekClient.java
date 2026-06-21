package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.config.DeepSeekProperties;
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
@ConditionalOnProperty(name = "llm.provider", havingValue = "deepseek")
public class DeepSeekClient implements LlmClient {

    private static final Logger log = LoggerFactory.getLogger(DeepSeekClient.class);
    private static final int MAX_TOOL_ITERATIONS = 5;

    private final WebClient webClient;
    private final ObjectMapper objectMapper;
    private final McpClient mcpClient;
    private final ChatHistoryService chatHistoryService;
    private final FaqEmbeddingService faqEmbeddingService;
    private final String model;

    public DeepSeekClient(DeepSeekProperties properties, ObjectMapper objectMapper,
                          McpClient mcpClient, ChatHistoryService chatHistoryService,
                          FaqEmbeddingService faqEmbeddingService) {
        this.objectMapper = objectMapper;
        this.mcpClient = mcpClient;
        this.chatHistoryService = chatHistoryService;
        this.faqEmbeddingService = faqEmbeddingService;
        this.model = properties.getModel();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Authorization", "Bearer " + properties.getApiKey())
                .defaultHeader("Content-Type", "application/json")
                .build();
    }

    @Override
    public String chat(String userMessage, long telegramUserId) {
        List<Map<String, Object>> messages = new ArrayList<>();

        String faqContext = faqEmbeddingService.buildFaqContext(userMessage);
        messages.add(Map.of("role", "system", "content",
                SupportPrompt.withFaqContext(faqContext, telegramUserId)));
        messages.addAll(chatHistoryService.getHistory(telegramUserId));
        messages.add(Map.of("role", "user", "content", userMessage));

        String response = processChat(messages, 0, userMessage);
        if (!isErrorResponse(response)) {
            chatHistoryService.addUserMessage(telegramUserId, userMessage);
            chatHistoryService.addAssistantMessage(telegramUserId, response);
        }
        return response;
    }

    @Override
    public String chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        return "DeepSeek не поддерживает обработку изображений. "
                + "Переключите провайдера на Gemini (LLM_PROVIDER=gemini) или опишите проблему текстом.";
    }

    private String processChat(List<Map<String, Object>> messages, int iteration,
                                 String originalUserMessage) {
        if (iteration >= MAX_TOOL_ITERATIONS) {
            return "Превышено количество попыток обработки запроса. Пожалуйста, попробуйте ещё раз.";
        }

        List<Map<String, Object>> functionDefinitions = buildFunctionDefinitions();

        try {
            ObjectNode requestBody = buildRequestBody(messages, functionDefinitions);

            log.debug("DeepSeek request (iteration {}): {} tools available", iteration, functionDefinitions.size());

            String response = webClient.post()
                    .uri("/chat/completions")
                    .bodyValue(requestBody)
                    .retrieve()
                    .onStatus(status -> status.is4xxClientError() || status.is5xxServerError(),
                            clientResponse -> clientResponse.bodyToMono(String.class)
                                    .flatMap(err -> Mono.error(new RuntimeException(
                                            "DeepSeek API error: " + clientResponse.statusCode() + " - " + err))))
                    .bodyToMono(String.class)
                    .block();

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode choices = jsonResponse.get("choices");
            if (choices == null || !choices.isArray() || choices.isEmpty()) {
                log.error("Empty choices in DeepSeek response: {}", response);
                return "Не удалось получить ответ от модели. Попробуйте позже.";
            }

            JsonNode message = choices.get(0).get("message");
            String content = message.has("content") && !message.get("content").isNull()
                    ? message.get("content").asText()
                    : null;
            JsonNode toolCalls = message.get("tool_calls");

            if (toolCalls != null && toolCalls.isArray() && toolCalls.size() > 0) {
                log.info("DeepSeek requested {} tool call(s)", toolCalls.size());

                messages.add(Map.of("role", "assistant", "tool_calls", objectMapper.convertValue(toolCalls, List.class)));

                for (JsonNode toolCall : toolCalls) {
                    String functionName = toolCall.get("function").get("name").asText();
                    String argumentsStr = toolCall.get("function").get("arguments").asText();
                    String toolCallId = toolCall.get("id").asText();

                    @SuppressWarnings("unchecked")
                    Map<String, Object> arguments = argumentsStr.isEmpty()
                            ? Map.of()
                            : objectMapper.readValue(argumentsStr, Map.class);

                    log.info("Executing tool: {} with args: {}", functionName, arguments);

                    String toolResult = mcpClient.callTool(functionName, arguments);
                    log.info("Tool {} result: {}",
                            functionName,
                            toolResult.length() > 300 ? toolResult.substring(0, 300) + "..." : toolResult);

                    messages.add(Map.of(
                            "role", "tool",
                            "tool_call_id", toolCallId,
                            "content", toolResult
                    ));
                }

                if (iteration == 0) {
                    List<String> toolResults = messages.stream()
                            .filter(m -> "tool".equals(m.get("role")))
                            .map(m -> (String) m.get("content"))
                            .toList();
                    String refreshedFaq = faqEmbeddingService.buildRefinedFaqContext(
                            originalUserMessage, toolResults);
                    if (!refreshedFaq.isEmpty()) {
                        messages.add(Map.of("role", "user", "content",
                                "[Система] Результат диагностики получен. Актуальный FAQ:\n\n"
                                        + refreshedFaq));
                    }
                }

                return processChat(messages, iteration + 1, originalUserMessage);
            }

            if (content != null && !content.isEmpty()) {
                return content;
            }

            return "Модель не вернула ответа. Попробуйте переформулировать вопрос.";

        } catch (Exception e) {
            log.error("DeepSeek request failed", e);
            return "Произошла ошибка при обработке запроса. Попробуйте позже.";
        }
    }

    private boolean isErrorResponse(String response) {
        return response.startsWith("Превышено количество")
                || response.startsWith("Не удалось")
                || response.startsWith("Произошла ошибка")
                || response.startsWith("Модель не вернула");
    }

    private List<Map<String, Object>> buildFunctionDefinitions() {
        List<McpTool> tools = mcpClient.listTools();
        List<Map<String, Object>> functions = new ArrayList<>();

        for (McpTool tool : tools) {
            Map<String, Object> function = new java.util.LinkedHashMap<>();
            function.put("name", tool.getName());
            function.put("description", tool.getDescription());

            Map<String, Object> parameters = tool.getInputSchema();
            if (parameters == null || parameters.isEmpty()) {
                parameters = Map.of("type", "object", "properties", Map.of());
            }
            function.put("parameters", parameters);

            functions.add(Map.of("type", "function", "function", function));
        }

        return functions;
    }

    private ObjectNode buildRequestBody(List<Map<String, Object>> messages,
                                         List<Map<String, Object>> tools) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("model", model);

        ArrayNode messagesArray = body.putArray("messages");
        for (Map<String, Object> msg : messages) {
            messagesArray.add(objectMapper.valueToTree(msg));
        }

        if (!tools.isEmpty()) {
            ArrayNode toolsArray = body.putArray("tools");
            for (Map<String, Object> tool : tools) {
                toolsArray.add(objectMapper.valueToTree(tool));
            }
            body.put("tool_choice", "auto");
        }

        body.put("temperature", 0.3);

        return body;
    }
}
