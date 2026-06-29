package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.request.SendMessage;
import com.pengrad.telegrambot.request.SendPhoto;
import com.pengrad.telegrambot.response.SendResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

@Component
public class TelegramMessageSender {

    private static final Logger log = LoggerFactory.getLogger(TelegramMessageSender.class);
    private static final int MAX_MESSAGE_LENGTH = 4096;

    private final TelegramBot telegramBot;

    public TelegramMessageSender(TelegramBot telegramBot) {
        this.telegramBot = telegramBot;
    }

    public void send(long chatId, String text) {
        send(chatId, null, text);
    }

    public void sendToTopic(long chatId, int topicId, String text) {
        send(chatId, topicId, text);
    }

    private void send(long chatId, Integer topicId, String text) {
        if (text == null || text.isBlank()) {
            return;
        }
        for (String chunk : split(text)) {
            SendMessage request = new SendMessage(chatId, chunk);
            if (topicId != null) {
                request.messageThreadId(topicId);
            }
            execute(chatId, request);
        }
    }

    public void sendReply(long chatId, int replyToMessageId, String text) {
        List<String> chunks = split(text);
        for (int i = 0; i < chunks.size(); i++) {
            SendMessage request = new SendMessage(chatId, chunks.get(i));
            if (i == 0) {
                request.replyToMessageId(replyToMessageId);
            }
            execute(chatId, request);
        }
    }

    public void sendPhoto(long chatId, String imageRef) {
        if (imageRef == null || imageRef.isBlank()) {
            return;
        }
        try {
            if (isTelegramFileId(imageRef)) {
                SendResponse response = telegramBot.execute(new SendPhoto(chatId, imageRef));
                if (!response.isOk()) {
                    log.error("Failed to send photo to {}: {}", chatId, response.description());
                }
            } else {
                byte[] imageData = loadImage(imageRef);
                if (imageData == null) {
                    log.error("Image not found on classpath: faq/images/{}", imageRef);
                    return;
                }
                SendResponse response = telegramBot.execute(new SendPhoto(chatId, imageData));
                if (!response.isOk()) {
                    log.error("Failed to send photo to {}: {}", chatId, response.description());
                }
            }
        } catch (Exception e) {
            log.error("Error sending photo to {}", chatId, e);
        }
    }

    private boolean isTelegramFileId(String s) {
        return s.startsWith("AgAC") || s.startsWith("BQAC") || s.startsWith("AAMC");
    }

    private byte[] loadImage(String filename) {
        String path = "/faq/images/" + filename;
        try (InputStream is = getClass().getResourceAsStream(path)) {
            if (is == null) return null;
            return is.readAllBytes();
        } catch (IOException e) {
            log.error("Failed to read image from classpath: {}", path, e);
            return null;
        }
    }

    private void execute(long chatId, SendMessage request) {
        try {
            SendResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.error("Failed to send message to {}: {}", chatId, response.description());
            }
        } catch (Exception e) {
            log.error("Error sending message to {}", chatId, e);
        }
    }

    static List<String> split(String text) {
        if (text == null || text.isEmpty()) {
            return List.of("");
        }
        if (text.length() <= MAX_MESSAGE_LENGTH) {
            return List.of(text);
        }

        List<String> chunks = new ArrayList<>();
        int offset = 0;
        while (offset < text.length()) {
            int end = Math.min(offset + MAX_MESSAGE_LENGTH, text.length());
            if (end < text.length()) {
                int breakAt = text.lastIndexOf('\n', end);
                if (breakAt <= offset) {
                    breakAt = end;
                }
                end = breakAt;
            }
            chunks.add(text.substring(offset, end));
            offset = end;
            if (offset < text.length() && text.charAt(offset) == '\n') {
                offset++;
            }
        }
        return chunks;
    }
}
