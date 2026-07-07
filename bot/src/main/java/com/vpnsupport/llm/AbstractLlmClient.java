package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public abstract class AbstractLlmClient implements LlmClient {

    protected final Logger log = LoggerFactory.getLogger(getClass());

    protected static final int MAX_TOOL_ITERATIONS = 5;
    private static final int TOOL_RESULT_MAX_LOG_LENGTH = 2000;

    protected final ObjectMapper objectMapper;
    protected final McpRouter mcpRouter;
    protected final ChatHistoryService chatHistoryService;
    protected final FaqEmbeddingService faqEmbeddingService;
    protected final LlmTokenUsageRepository tokenUsageRepository;

    protected AbstractLlmClient(ObjectMapper objectMapper, McpRouter mcpRouter,
                                ChatHistoryService chatHistoryService,
                                FaqEmbeddingService faqEmbeddingService,
                                LlmTokenUsageRepository tokenUsageRepository) {
        this.objectMapper = objectMapper;
        this.mcpRouter = mcpRouter;
        this.chatHistoryService = chatHistoryService;
        this.faqEmbeddingService = faqEmbeddingService;
        this.tokenUsageRepository = tokenUsageRepository;
    }

    @Override
    public String chat(String userMessage, long telegramUserId) {
        String response = doChat(userMessage, telegramUserId, null, null);
        chatHistoryService.addUserMessage(telegramUserId, userMessage);
        chatHistoryService.addAssistantMessage(telegramUserId, response);
        return response;
    }

    @Override
    public String chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        if (!supportsImages()) {
            throw new LlmProcessingException("Image not supported",
                    getProviderName() + " не поддерживает обработку изображений. Опишите проблему текстом.");
        }
        String response = doChat(userMessage, telegramUserId, base64Image, mimeType);
        String historyMessage = userMessage != null && !userMessage.isBlank() ? userMessage : "[Скриншот]";
        chatHistoryService.addUserMessage(telegramUserId, historyMessage);
        chatHistoryService.addAssistantMessage(telegramUserId, response);
        return response;
    }

    protected String doChat(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        int iteration = 0;
        String faqContext = faqEmbeddingService.buildFaqContext(userMessage);
        List<Map<String, Object>> conversation = buildInitialConversation(
                userMessage, telegramUserId, faqContext, base64Image, mimeType);

        while (iteration < MAX_TOOL_ITERATIONS) {
            try {
                String rawResponse = callApi(conversation, faqContext, telegramUserId);
                saveUsage(rawResponse, telegramUserId);
                LlmResponse llmResponse = parseResponse(rawResponse);

                if (llmResponse.hasToolCalls()) {
                    log.info("{} requested {} tool call(s)", getProviderName(), llmResponse.toolCalls().size());
                    addToolCallsToConversation(conversation, llmResponse);

                    for (LlmResponse.ToolCall tc : llmResponse.toolCalls()) {
                        log.info("Executing tool: {} with args: {}", tc.name(), tc.arguments());
                        String toolResult = mcpRouter.callTool(tc.name(), tc.arguments());
                        log.info("Tool {} result: {}", tc.name(), truncate(toolResult));
                        addToolResultToConversation(conversation, tc, toolResult);
                    }

                    iteration++;
                    continue;
                }

                if (llmResponse.text() != null && !llmResponse.text().isEmpty()) {
                    return llmResponse.text();
                }

                throw new LlmProcessingException("No content returned",
                        "Модель не вернула ответа. Попробуйте переформулировать вопрос.");

            } catch (LlmProcessingException e) {
                throw e;
            } catch (Exception e) {
                log.error("{} request failed", getProviderName(), e);
                throw new LlmProcessingException(e.getMessage(),
                        "Произошла ошибка при обработке запроса. Попробуйте позже.", e);
            }
        }

        throw new LlmProcessingException("Max iterations reached",
                "Превышено количество попыток обработки запроса. Пожалуйста, попробуйте ещё раз.");
    }

    protected abstract List<Map<String, Object>> buildInitialConversation(
            String userMessage, long telegramUserId, String faqContext,
            String base64Image, String mimeType);

    protected abstract String callApi(List<Map<String, Object>> conversation, String faqContext, long telegramUserId);

    protected abstract LlmResponse parseResponse(String rawResponse);

    protected abstract void addToolCallsToConversation(List<Map<String, Object>> conversation, LlmResponse response);

    protected abstract void addToolResultToConversation(List<Map<String, Object>> conversation,
                                                          LlmResponse.ToolCall toolCall, String toolResult);

    protected abstract void saveUsage(String rawResponse, long telegramUserId);

    protected abstract String getProviderName();

    protected static String truncate(String s) {
        return s != null && s.length() > TOOL_RESULT_MAX_LOG_LENGTH ? s.substring(0, TOOL_RESULT_MAX_LOG_LENGTH) + "..." : s;
    }
}
