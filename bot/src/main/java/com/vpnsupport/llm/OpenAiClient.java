package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsage;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.OpenAiProperties;
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
@ConditionalOnProperty(name = "llm.provider", havingValue = "openai")
public class OpenAiClient extends AbstractLlmClient {

    private static final Logger log = LoggerFactory.getLogger(OpenAiClient.class);

    private final WebClient webClient;
    private final String model;
    private final Double temperature;
    private volatile List<Map<String, Object>> cachedToolDefinitions;

    public OpenAiClient(OpenAiProperties properties, ObjectMapper objectMapper,
                        McpRouter mcpRouter, ChatHistoryService chatHistoryService,
                        FaqEmbeddingService faqEmbeddingService,
                        LlmTokenUsageRepository tokenUsageRepository) {
        super(objectMapper, mcpRouter, chatHistoryService, faqEmbeddingService, tokenUsageRepository);
        String apiKey = properties.getApiKey();
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalArgumentException("OpenAI API key must not be null or blank");
        }
        this.model = properties.getModel();
        this.temperature = properties.getTemperature();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Authorization", "Bearer " + apiKey)
                .defaultHeader("Content-Type", "application/json")
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(
                        HttpClient.create().responseTimeout(Duration.ofSeconds(60))))
                .build();
    }

    private List<Map<String, Object>> getToolDefinitions() {
        List<Map<String, Object>> tools = cachedToolDefinitions;
        if (tools == null) {
            synchronized (this) {
                tools = cachedToolDefinitions;
                if (tools == null) {
                    cachedToolDefinitions = tools = buildToolDefinitions();
                }
            }
        }
        return tools;
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
        List<Map<String, Object>> messages = new ArrayList<>();
        messages.add(Map.of("role", "system", "content", SupportPrompt.SYSTEM));

        String dynamicContext = "Telegram ID: " + telegramUserId;
        if (faqContext != null && !faqContext.isEmpty()) {
            dynamicContext += "\n\n" + faqContext;
        }
        messages.add(Map.of("role", "system", "content", dynamicContext));

        messages.addAll(chatHistoryService.getHistory(telegramUserId));

        if (base64Image != null && !base64Image.isEmpty()) {
            List<Object> parts = new ArrayList<>();
            if (userMessage != null && !userMessage.isBlank()) {
                parts.add(Map.of("type", "input_text", "text", userMessage));
            }
            String dataUri = "data:" + (mimeType != null ? mimeType : "image/jpeg") + ";base64," + base64Image;
            parts.add(Map.of(
                    "type", "input_image",
                    "image_url", dataUri
            ));
            messages.add(Map.of("role", "user", "content", parts));
        } else {
            messages.add(Map.of("role", "user", "content", userMessage));
        }

        return messages;
    }

    @Override
    @SuppressWarnings("unused")
    protected String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId) {
        ObjectNode requestBody = objectMapper.createObjectNode();
        requestBody.put("model", model);

        ArrayNode inputArray = requestBody.putArray("input");
        for (Map<String, Object> item : conversation) {
            inputArray.add(objectMapper.valueToTree(item));
        }

        List<Map<String, Object>> tools = getToolDefinitions();
        if (!tools.isEmpty()) {
            ArrayNode toolsArray = requestBody.putArray("tools");
            for (Map<String, Object> tool : tools) {
                toolsArray.add(objectMapper.valueToTree(tool));
            }
            requestBody.put("tool_choice", "auto");
            ObjectNode reasoning = requestBody.putObject("reasoning");
            reasoning.put("effort", "none");
        }

        if (temperature != null) {
            requestBody.put("temperature", temperature);
        }

        log.debug("OpenAI Responses API request ({} tools available)", getToolDefinitions().size());

        String rawResponse = webClient.post()
                .uri("/responses")
                .bodyValue(requestBody)
                .retrieve()
                .onStatus(status -> status.is4xxClientError() || status.is5xxServerError(),
                        clientResponse -> clientResponse.bodyToMono(String.class)
                                .flatMap(err -> {
                                    if (clientResponse.statusCode().value() == 401) {
                                        return Mono.error(new RuntimeException(
                                                "OpenAI API error (model=" + model + "): "
                                                + clientResponse.statusCode() + " - " + err
                                                + " | Проверьте OPENAI_API_KEY и OPENAI_MODEL в .env"));
                                    }
                                    return Mono.error(new RuntimeException(
                                            "OpenAI API error (model=" + model + "): "
                                            + clientResponse.statusCode() + " - " + err));
                                }))
                .bodyToMono(String.class)
                .block();

        if (rawResponse == null) {
            throw new RuntimeException("OpenAI Responses API returned null/empty body");
        }

        return rawResponse;
    }

    @Override
    protected LlmResponse parseResponse(String rawResponse) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode output = jsonResponse.get("output");
            if (output == null || !output.isArray()) {
                log.error("No output array in OpenAI Responses API response: {}", rawResponse);
                throw new LlmProcessingException("Empty output",
                        "Не удалось получить ответ от модели. Попробуйте позже.");
            }

            StringBuilder text = new StringBuilder();
            List<LlmResponse.ToolCall> toolCalls = new ArrayList<>();

            for (JsonNode item : output) {
                String type = item.has("type") ? item.get("type").asText() : "";
                if ("function_call".equals(type)) {
                    String fnName = item.get("name").asText();
                    String fnArgsStr = item.get("arguments").asText();
                    String callId = item.get("call_id").asText();
                    @SuppressWarnings("unchecked")
                    Map<String, Object> args = fnArgsStr.isEmpty()
                            ? Map.of()
                            : objectMapper.readValue(fnArgsStr, Map.class);
                    toolCalls.add(new LlmResponse.ToolCall(fnName, callId, args));
                } else if ("message".equals(type)) {
                    JsonNode content = item.get("content");
                    if (content != null && content.isArray()) {
                        for (JsonNode part : content) {
                            if ("output_text".equals(part.get("type").asText())
                                    && part.has("text") && !part.get("text").isNull()) {
                                text.append(part.get("text").asText());
                            }
                        }
                    }
                }
            }

            if (text.isEmpty() && toolCalls.isEmpty()) {
                log.warn("No text or tool calls in OpenAI response. output={}", rawResponse);
                throw new LlmProcessingException("Empty response",
                        "Модель не вернула ответа. Попробуйте переформулировать вопрос.");
            }

            return new LlmResponse(text.toString(), toolCalls);
        } catch (LlmProcessingException e) {
            throw e;
        } catch (Exception e) {
            log.error("Failed to parse OpenAI response: {} — response: {}", e.getMessage(), rawResponse);
            throw new LlmProcessingException("Parse error: " + e.getMessage(),
                    "Ошибка обработки ответа модели.");
        }
    }

    @Override
    protected void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response) {
        for (LlmResponse.ToolCall tc : response.toolCalls()) {
            String argsJson;
            try {
                argsJson = objectMapper.writeValueAsString(tc.arguments());
            } catch (Exception e) {
                argsJson = "{}";
            }
            conversation.add(Map.of(
                    "type", "function_call",
                    "call_id", tc.id(),
                    "name", tc.name(),
                    "arguments", argsJson
            ));
        }
    }

    @Override
    protected void addToolResultToConversation(List<Map<String, Object>> conversation,
                                                LlmResponse.ToolCall toolCall, String toolResult) {
        conversation.add(Map.of(
                "type", "function_call_output",
                "call_id", toolCall.id(),
                "output", toolResult
        ));
    }

    @Override
    protected void saveUsage(String rawResponse, long telegramUserId) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode usage = jsonResponse.get("usage");
            if (usage != null) {
                long promptTokens = usage.has("input_tokens") ? usage.get("input_tokens").asLong()
                        : usage.has("prompt_tokens") ? usage.get("prompt_tokens").asLong() : 0;
                long completionTokens = usage.has("output_tokens") ? usage.get("output_tokens").asLong()
                        : usage.has("completion_tokens") ? usage.get("completion_tokens").asLong() : 0;
                long totalTokens = usage.has("total_tokens") ? usage.get("total_tokens").asLong()
                        : promptTokens + completionTokens;
                tokenUsageRepository.save(new LlmTokenUsage(
                        telegramUserId, promptTokens, completionTokens, totalTokens));
            }
        } catch (Exception e) {
            log.warn("Failed to save token usage: {}", e.getMessage());
        }
    }

    @Override
    protected String getProviderName() {
        return "OpenAI";
    }

    private List<Map<String, Object>> buildToolDefinitions() {
        List<McpTool> tools = mcpRouter.listTools();
        List<Map<String, Object>> functions = new ArrayList<>();
        for (McpTool tool : tools) {
            Map<String, Object> fn = new LinkedHashMap<>();
            fn.put("type", "function");
            fn.put("name", tool.name());
            fn.put("description", tool.description());
            Map<String, Object> parameters = tool.inputSchema();
            if (parameters == null || parameters.isEmpty()) {
                parameters = Map.of("type", "object", "properties", Map.of());
            }
            fn.put("parameters", parameters);
            functions.add(fn);
        }
        return List.copyOf(functions);
    }
}
