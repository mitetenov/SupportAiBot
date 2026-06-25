package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.request.SendMessage;
import com.pengrad.telegrambot.response.SendResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

@Component
public class AdminNotifier {

    private static final Logger log = LoggerFactory.getLogger(AdminNotifier.class);
    private static final int MAX_ERROR_LENGTH = 2000;

    private final TelegramBot telegramBot;
    private final long supportGroupChatId;

    public AdminNotifier(TelegramBot telegramBot, com.vpnsupport.config.TelegramProperties telegramProperties) {
        this.telegramBot = telegramBot;
        this.supportGroupChatId = telegramProperties.getSupportGroupChatId();
    }

    public void notifyError(String context, Throwable error) {
        notifyError(context, null, error);
    }

    public void notifyError(String context, Long userId, Throwable error) {
        String rawMessage = error.getMessage();
        String errorMessage = rawMessage != null
                ? rawMessage.substring(0, Math.min(rawMessage.length(), MAX_ERROR_LENGTH))
                : "null";

        StringBuilder sb = new StringBuilder("[ОШИБКА БОТА]\n");
        sb.append(context).append("\n");
        if (userId != null) {
            sb.append("User: ").append(userId).append("\n");
        }
        sb.append("\n").append(errorMessage);

        String text = sb.toString();
        try {
            SendMessage request = new SendMessage(supportGroupChatId, text);
            request.disableNotification(true);
            SendResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.warn("Failed to send admin error notification: {}", response.description());
            }
        } catch (Exception e) {
            log.warn("Failed to send admin error notification", e);
        }
    }
}
