package com.vpnsupport.bot;

import com.pengrad.telegrambot.model.User;
import com.vpnsupport.bot.UserMessageBuffer.MessageBatch;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.llm.LlmProcessingException;
import com.vpnsupport.llm.LlmReply;
import com.vpnsupport.support.SupportGroupForwarder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Answers one batch of user messages: rate limiting, the model call, delivery,
 * forwarding to the support topic and knowledge-gap accounting.
 *
 * <p>Separate from {@code VpnSupportBot} so the answer path can be exercised
 * directly, without going through Telegram polling or the debounce timer.
 */
@Component
public class UserMessagePipeline {

    private static final Logger log = LoggerFactory.getLogger(UserMessagePipeline.class);
    private static final int MAX_ERROR_FORWARD_LENGTH = 3000;

    private final LlmClient llmClient;
    private final TelegramMessageSender messageSender;
    private final SupportGroupForwarder forwarder;
    private final UserRateLimiter rateLimiter;
    private final KnowledgeGapService knowledgeGapService;
    private final ConversationState conversationState;
    private final TypingIndicator typingIndicator;
    private final BotMessages messages;

    public UserMessagePipeline(LlmClient llmClient,
                               TelegramMessageSender messageSender,
                               SupportGroupForwarder forwarder,
                               UserRateLimiter rateLimiter,
                               KnowledgeGapService knowledgeGapService,
                               ConversationState conversationState,
                               TypingIndicator typingIndicator,
                               BotMessages messages) {
        this.llmClient = llmClient;
        this.messageSender = messageSender;
        this.forwarder = forwarder;
        this.rateLimiter = rateLimiter;
        this.knowledgeGapService = knowledgeGapService;
        this.conversationState = conversationState;
        this.typingIndicator = typingIndicator;
        this.messages = messages;
    }

    public void handle(MessageBatch batch) {
        long chatId = batch.lastMessage().chat().id();
        User user = batch.user();
        String text = batch.text();

        if (conversationState.isOperatorRecentlyActive(user.id())) {
            forwarder.forwardToSupport(chatId, batch.messageIds(), user,
                    messages.get("support.ai.suppressed"), false);
            return;
        }

        // The buffer has already merged a typing burst into this one batch, so
        // tripping here means sustained flooding. Forward regardless: a user
        // message must never be silently dropped.
        if (!rateLimiter.tryAcquire(user.id())) {
            messageSender.send(chatId, messages.get("bot.ratelimit.wait"));
            forwarder.forwardToSupport(chatId, batch.messageIds(), user,
                    messages.get("support.ratelimited"), true);
            return;
        }

        try (TypingIndicator.Session ignored = typingIndicator.start(chatId)) {
            LlmReply reply = batch.hasImage()
                    ? llmClient.chatWithImage(text, user.id(), batch.base64Image(), batch.mimeType())
                    : llmClient.chat(text, user.id());

            conversationState.recordQuery(user.id(), text, reply.faqContext());

            String response = EscalationPolicy.stripMarker(reply.text());
            if (response.isEmpty()) {
                response = messages.get("bot.llm.empty");
            }
            messageSender.send(chatId, response);

            boolean escalate = EscalationPolicy.modelRequestedEscalation(reply.text())
                    || EscalationPolicy.userRequestsHuman(text);

            forwarder.forwardToSupport(chatId, batch.messageIds(), user, response, escalate);
            knowledgeGapService.evaluate(text, user.id(), reply.text(), reply.faqContext());

        } catch (LlmProcessingException e) {
            log.error("LLM error processing message from user {}", user.id(), e);
            reportFailure(batch, user, e.getUserFriendlyMessage(), e);
        } catch (Exception e) {
            log.error("Error processing message from user {}", user.id(), e);
            reportFailure(batch, user, messages.get("bot.llm.error"), e);
        }
    }

    private void reportFailure(MessageBatch batch, User user, String userVisibleMessage, Exception cause) {
        messageSender.send(batch.lastMessage().chat().id(), userVisibleMessage);
        forwarder.forwardErrorToTopic(user, batch.text(), userVisibleMessage, extractErrorMessage(cause));
    }

    private static String extractErrorMessage(Exception e) {
        String msg = e.getMessage();
        if (msg != null && msg.length() > MAX_ERROR_FORWARD_LENGTH) {
            msg = msg.substring(0, MAX_ERROR_FORWARD_LENGTH) + "...";
        }
        return "Bot: " + (msg != null ? msg : e.getClass().getSimpleName());
    }
}
