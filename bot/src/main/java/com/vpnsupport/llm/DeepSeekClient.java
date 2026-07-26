package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsage;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.config.DeepSeekProperties;
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
@ConditionalOnProperty(name = "llm.provider", havingValue = "deepseek")
public class DeepSeekClient extends AbstractLlmClient {

    private static final Logger log = LoggerFactory.getLogger(DeepSeekClient.class);
    private static final double TEMPERATURE = 0.3;

    private final WebClient webClient;
    private final String model;
    private volatile List<Map<String, Object>> cachedToolDefinitions;

    public DeepSeekClient(DeepSeekProperties properties, ObjectMapper objectMapper,
                          McpRouter mcpRouter, ChatHistoryService chatHistoryService,
                          FaqEmbeddingService faqEmbeddingService,
                          LlmTokenUsageRepository tokenUsageRepository) {
        super(objectMapper, mcpRouter, chatHistoryService, faqEmbeddingService, tokenUsageRepository);
        this.model = properties.getModel();
        this.webClient = WebClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("Authorization", "Bearer " + properties.getApiKey())
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
        messages.add(Map.of("role", "user", "content", userMessage));
        return messages;
    }

    @Override
    @SuppressWarnings("unused")
    protected String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId) {
        ObjectNode requestBody = buildRequestBody(conversation);
        log.debug("DeepSeek request ({} tools available)", getToolDefinitions().size());

        return webClient.post()
                .uri("/chat/completions")
                .bodyValue(requestBody)
                .retrieve()
                .onStatus(status -> status.is4xxClientError() || status.is5xxServerError(),
                        clientResponse -> clientResponse.bodyToMono(String.class)
                                .flatMap(err -> Mono.error(new RuntimeException(
                                        "DeepSeek API error: " + clientResponse.statusCode() + " - " + err))))
                .bodyToMono(String.class)
                .block();
    }

    @Override
    protected LlmResponse parseResponse(String rawResponse) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode choices = jsonResponse.get("choices");
            if (choices == null || !choices.isArray() || choices.isEmpty()) {
                log.error("Empty choices in DeepSeek response: {}", rawResponse);
                throw new LlmProcessingException("Empty choices",
                        "Не удалось получить ответ от модели. Попробуйте позже.");
            }

            JsonNode message = choices.get(0).get("message");
            if (message == null) {
                throw new LlmProcessingException("No message in response",
                        "Не удалось получить ответ от модели. Попробуйте позже.");
            }

            String content = message.has("content") && !message.get("content").isNull()
                    ? message.get("content").asText() : null;

            List<LlmResponse.ToolCall> toolCalls = new ArrayList<>();
            JsonNode toolCallsNode = message.get("tool_calls");
            if (toolCallsNode != null && toolCallsNode.isArray()) {
                for (JsonNode tc : toolCallsNode) {
                    String fnName = tc.get("function").get("name").asText();
                    String fnArgsStr = tc.get("function").get("arguments").asText();
                    String tcId = tc.get("id").asText();
                    @SuppressWarnings("unchecked")
                    Map<String, Object> args = fnArgsStr.isEmpty()
                            ? Map.of()
                            : objectMapper.readValue(fnArgsStr, Map.class);
                    toolCalls.add(new LlmResponse.ToolCall(fnName, tcId, args));
                }
            }

            return new LlmResponse(content != null ? content : "", toolCalls);
        } catch (LlmProcessingException e) {
            throw e;
        } catch (Exception e) {
            throw new LlmProcessingException("Parse error",
                    "Ошибка обработки ответа модели.");
        }
    }

    @Override
    protected void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response) {
        List<Map<String, Object>> toolCallMaps = new ArrayList<>();
        for (LlmResponse.ToolCall tc : response.toolCalls()) {
            toolCallMaps.add(Map.of(
                    "id", tc.id(),
                    "type", "function",
                    // The OpenAI-compatible schema types function.arguments as a
                    // JSON *string*; sending an object breaks the second turn of
                    // the tool loop.
                    "function", Map.of("name", tc.name(), "arguments", serializeArguments(tc.arguments()))
            ));
        }
        conversation.add(Map.of("role", "assistant", "tool_calls", toolCallMaps));
    }

    private String serializeArguments(Map<String, Object> arguments) {
        try {
            return objectMapper.writeValueAsString(arguments != null ? arguments : Map.of());
        } catch (Exception e) {
            log.warn("Failed to serialize tool arguments, sending empty object: {}", e.getMessage());
            return "{}";
        }
    }

    @Override
    protected void addToolResultToConversation(List<Map<String, Object>> conversation,
                                                LlmResponse.ToolCall toolCall, String toolResult) {
        conversation.add(Map.of("role", "tool", "tool_call_id", toolCall.id(), "content", toolResult));
    }



    @Override
    protected void saveUsage(String rawResponse, long telegramUserId) {
        try {
            JsonNode jsonResponse = objectMapper.readTree(rawResponse);
            JsonNode usage = jsonResponse.get("usage");
            if (usage != null) {
                long promptTokens = usage.get("prompt_tokens").asLong();
                long completionTokens = usage.get("completion_tokens").asLong();
                long totalTokens = usage.get("total_tokens").asLong();
                tokenUsageRepository.save(new LlmTokenUsage(
                        telegramUserId, promptTokens, completionTokens, totalTokens));
            }
        } catch (Exception e) {
            log.warn("Failed to save token usage: {}", e.getMessage());
        }
    }

    @Override
    protected String getProviderName() {
        return "DeepSeek";
    }

    private List<Map<String, Object>> buildToolDefinitions() {
        List<McpTool> tools = mcpRouter.listTools();
        List<Map<String, Object>> functions = new ArrayList<>();
        for (McpTool tool : tools) {
            Map<String, Object> function = new LinkedHashMap<>();
            function.put("name", tool.name());
            function.put("description", tool.description());
            Map<String, Object> parameters = tool.inputSchema();
            if (parameters == null || parameters.isEmpty()) {
                parameters = Map.of("type", "object", "properties", Map.of());
            }
            function.put("parameters", parameters);
            functions.add(Map.of("type", "function", "function", function));
        }
        return List.copyOf(functions);
    }

    private ObjectNode buildRequestBody(List<Map<String, Object>> messages) {
        ObjectNode body = objectMapper.createObjectNode();
        body.put("model", model);

        ArrayNode messagesArray = body.putArray("messages");
        for (Map<String, Object> msg : messages) {
            messagesArray.add(objectMapper.valueToTree(msg));
        }

        List<Map<String, Object>> tools = getToolDefinitions();
        if (!tools.isEmpty()) {
            ArrayNode toolsArray = body.putArray("tools");
            for (Map<String, Object> tool : tools) {
                toolsArray.add(objectMapper.valueToTree(tool));
            }
            body.put("tool_choice", "auto");
        }

        body.put("temperature", TEMPERATURE);
        return body;
    }

    @Override
    @SuppressWarnings("unused")
    public LlmReply chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        throw new LlmProcessingException("Image not supported",
                "DeepSeek не поддерживает обработку изображений. Переключите провайдера на Gemini (LLM_PROVIDER=gemini) или опишите проблему текстом.");
    }
}
