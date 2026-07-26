package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.ChatHistoryService;
import com.vpnsupport.bot.LlmTokenUsageRepository;
import com.vpnsupport.bot.RejectionDetector;
import com.vpnsupport.rag.FaqEmbeddingService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

public abstract class AbstractLlmClient implements LlmClient {

    protected final Logger log = LoggerFactory.getLogger(getClass());

    protected static final int MAX_TOOL_ITERATIONS = 5;
    private static final int TOOL_RESULT_MAX_LOG_LENGTH = 2000;

    /**
     * A message that carries no subject of its own and only makes sense against
     * the previous turn: a bare pronoun/adverb reference, an agreement, or a
     * platform name dropped as an answer to "which device?".
     *
     * <p>Deliberately narrower than the previous "shorter than 35 characters"
     * rule, which swept up self-contained questions — "Как оплатить?" is 13
     * characters and got concatenated with whatever unrelated thing the user
     * asked before, dragging the search away from the right FAQ entry.
     */
    private static final Pattern FOLLOW_UP = Pattern.compile(
            "^(а|и|но|ну)\\s"                                  // continuation particle
            + "|^(да|нет|ага|угу|ок|окей|понял|поняла)\\b"      // acknowledgement
            + "|\\b(это|этот|эта|туда|там|тут|оно|его|её|нём|ним|такое)\\b"  // anaphora
            + "|^(айфон|iphone|андроид|android|винда|windows|макбук|мак|mac|linux|tv|телевизор)\\b",
            // UNICODE_CHARACTER_CLASS makes \b and \w Unicode-aware; without it
            // they are ASCII-only and no Cyrillic boundary ever matches.
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE | Pattern.UNICODE_CHARACTER_CLASS);

    /** A message with no letters at all (emoji, punctuation) can't stand alone either. */
    private static final Pattern HAS_LETTERS = Pattern.compile("\\p{L}");

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
    public LlmReply chat(String userMessage, long telegramUserId) {
        return respond(userMessage, telegramUserId, null, null, userMessage);
    }

    @Override
    public LlmReply chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        if (!supportsImages()) {
            throw new LlmProcessingException("Image not supported",
                    getProviderName() + " не поддерживает обработку изображений. Опишите проблему текстом.");
        }
        String historyMessage = userMessage != null && !userMessage.isBlank() ? userMessage : "[Скриншот]";
        return respond(userMessage, telegramUserId, base64Image, mimeType, historyMessage);
    }

    private LlmReply respond(String userMessage, long telegramUserId, String base64Image,
                             String mimeType, String historyMessage) {
        chatHistoryService.clearRejectedFaqsIfNewTopic(telegramUserId, userMessage);

        LlmReply reply = doChat(userMessage, telegramUserId, base64Image, mimeType);

        chatHistoryService.addUserMessage(telegramUserId, historyMessage);
        chatHistoryService.addAssistantMessage(telegramUserId, reply.text());
        chatHistoryService.addRejectedFaqQuestions(telegramUserId, reply.faqContext().questions());
        return reply;
    }

    protected LlmReply doChat(String userMessage, long telegramUserId, String base64Image, String mimeType) {
        int iteration = 0;

        String searchQuery = RejectionDetector.isRejection(userMessage)
                ? chatHistoryService.getLastUserMessage(telegramUserId)
                : buildContextualSearchQuery(telegramUserId, userMessage);
        if (searchQuery == null || searchQuery.isBlank()) {
            searchQuery = userMessage;
        }

        Set<String> rejectedFaqs = chatHistoryService.getRejectedFaqQuestions(telegramUserId);
        FaqEmbeddingService.FaqContext faqContext =
                faqEmbeddingService.buildFaqContext(searchQuery, rejectedFaqs);

        List<Map<String, Object>> conversation = buildInitialConversation(
                userMessage, telegramUserId, faqContext.text(), base64Image, mimeType);

        while (iteration < MAX_TOOL_ITERATIONS) {
            try {
                String rawResponse = callApi(conversation, faqContext.text(), telegramUserId);
                saveUsage(rawResponse, telegramUserId);
                LlmResponse llmResponse = parseResponse(rawResponse);

                if (llmResponse.hasToolCalls()) {
                    log.info("{} requested {} tool call(s)", getProviderName(), llmResponse.toolCalls().size());
                    addToolCallsToConversation(conversation, llmResponse);

                    for (LlmResponse.ToolCall tc : llmResponse.toolCalls()) {
                        log.info("Executing tool: {} with args: {}", tc.name(), tc.arguments());
                        String toolResult = mcpRouter.callTool(tc.name(), tc.arguments(), telegramUserId);
                        log.info("Tool {} result: {}", tc.name(), truncate(toolResult));
                        addToolResultToConversation(conversation, tc, toolResult);
                    }

                    iteration++;
                    continue;
                }

                if (llmResponse.text() != null && !llmResponse.text().isEmpty()) {
                    return new LlmReply(llmResponse.text(), faqContext);
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

    /**
     * Prefixes the previous user message when this one cannot be searched on its
     * own, so "а на айфоне?" retrieves against the topic it is continuing.
     */
    protected String buildContextualSearchQuery(long telegramUserId, String userMessage) {
        if (userMessage == null || userMessage.isBlank()) {
            return userMessage;
        }
        String lastMsg = chatHistoryService.getLastUserMessage(telegramUserId);
        if (lastMsg == null || lastMsg.isBlank() || lastMsg.equalsIgnoreCase(userMessage)) {
            return userMessage;
        }

        String trimmed = userMessage.trim();
        boolean isFollowUp = !HAS_LETTERS.matcher(trimmed).find()
                || FOLLOW_UP.matcher(trimmed.toLowerCase(Locale.ROOT)).find();

        return isFollowUp ? lastMsg + " " + trimmed : trimmed;
    }
}
