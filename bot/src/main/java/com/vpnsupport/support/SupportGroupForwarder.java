package com.vpnsupport.support;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.bot.MessageMapping;
import com.vpnsupport.bot.MessageMappingRepository;
import com.vpnsupport.bot.TelegramMessageSender;
import com.vpnsupport.bot.TopicManager;
import com.vpnsupport.config.TelegramProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

@Component
public class SupportGroupForwarder {

    private static final Logger log = LoggerFactory.getLogger(SupportGroupForwarder.class);
    private static final int SUPPORT_PREVIEW_MAX_LENGTH = 3500;

    private final TelegramBot telegramBot;
    private final TelegramMessageSender messageSender;
    private final TopicManager topicManager;
    private final MessageMappingRepository messageMappingRepository;
    private final long supportGroupChatId;
    private final String adminUsername;

    public SupportGroupForwarder(TelegramBot telegramBot, TelegramMessageSender messageSender,
                                  TopicManager topicManager,
                                  MessageMappingRepository messageMappingRepository,
                                  TelegramProperties properties) {
        this.telegramBot = telegramBot;
        this.messageSender = messageSender;
        this.topicManager = topicManager;
        this.messageMappingRepository = messageMappingRepository;
        this.supportGroupChatId = properties.getSupportGroupChatId();
        this.adminUsername = properties.getSupportAdminUsername();
    }

    public void forwardToSupport(long userChatId, int userMessageId, User user,
                                  String userMessageText, String botResponse,
                                  boolean needsEscalation) {
        String userName = resolveUserName(user);
        Integer topicId = topicManager.resolveTopicId(user.id(), userName);

        if (topicId == null) {
            log.warn("Cannot forward to support group: no topic for user {}", user.id());
            return;
        }

        boolean ok = forwardUserMessage(userChatId, userMessageId, topicId);
        if (!ok) {
            log.warn("Failed to forward to topic {}, recreating for user {}", topicId, user.id());
            topicId = topicManager.recreateStaleTopic(user.id(), userName, topicId);
            if (topicId == null) {
                log.error("Failed to recreate topic for user {}", user.id());
                return;
            }
            ok = forwardUserMessage(userChatId, userMessageId, topicId);
            if (!ok) {
                log.error("Still failed to forward after topic recreation for user {}", user.id());
                return;
            }
        }

        sendBotResponse(topicId, userName, botResponse, needsEscalation);
    }

    private boolean forwardUserMessage(long userChatId, int userMessageId, Integer topicId) {
        CopyMessage request = new CopyMessage(supportGroupChatId, userChatId, userMessageId);
        request.messageThreadId(topicId);
        try {
            MessageIdResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.warn("Failed to copy user message to topic {}: {}", topicId, response.description());
                return false;
            }
            // Save mapping so operator replies to this topic message can be
            // forwarded back as replies to the original user message.
            Integer topicMessageId = response.messageId();
            if (topicMessageId != null) {
                messageMappingRepository.save(
                        new MessageMapping(topicMessageId, topicId, userChatId, userMessageId));
            }
            return true;
        } catch (Exception e) {
            log.warn("Error copying user message to topic {}: {}", topicId, e.getMessage());
            return false;
        }
    }

    private void sendBotResponse(Integer topicId, String userName, String botResponse,
                                  boolean needsEscalation) {
        String adminTag = needsEscalation && adminUsername != null && !adminUsername.isBlank()
                ? "@" + adminUsername + " "
                : "";

        String header = adminTag + "Ответ бота для " + userName + ":\n\n";
        String truncated = botResponse.length() > SUPPORT_PREVIEW_MAX_LENGTH
                ? botResponse.substring(0, SUPPORT_PREVIEW_MAX_LENGTH) + "...\n\n(сообщение обрезано)"
                : botResponse;

        messageSender.sendToTopic(supportGroupChatId, topicId, header + truncated);
    }

    private String resolveUserName(User user) {
        if (user.username() != null && !user.username().isBlank()) {
            return "@" + user.username();
        }
        String name = user.firstName();
        if (user.lastName() != null && !user.lastName().isBlank()) {
            name += " " + user.lastName();
        }
        return name != null && !name.isBlank() ? name : "User " + user.id();
    }

    public void forwardErrorToTopic(User user, String userMessage, String userVisibleMessage, String errorDetails) {
        String userName = resolveUserName(user);
        Integer topicId = topicManager.resolveTopicId(user.id(), userName);
        if (topicId == null) {
            log.warn("Cannot forward error to support group: no topic for user {}", user.id());
            return;
        }

        String adminTag = adminUsername != null && !adminUsername.isBlank()
                ? "@" + adminUsername + " "
                : "";

        String truncatedUserMsg = userMessage.length() > 300
                ? userMessage.substring(0, 300) + "..."
                : userMessage;

        messageSender.sendToTopic(supportGroupChatId, topicId,
                "[ОШИБКА БОТА] " + userName + ": " + truncatedUserMsg + "\n\nБот ответил:\n" + userVisibleMessage);

        String truncated = errorDetails.length() > SUPPORT_PREVIEW_MAX_LENGTH
                ? errorDetails.substring(0, SUPPORT_PREVIEW_MAX_LENGTH) + "..."
                : errorDetails;

        messageSender.sendToTopic(supportGroupChatId, topicId,
                adminTag + "Детали ошибки:\n\n" + truncated);
    }
}
