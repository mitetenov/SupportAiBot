package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.reaction.ReactionType;
import com.pengrad.telegrambot.request.BaseRequest;
import com.pengrad.telegrambot.request.EditMessageCaption;
import com.pengrad.telegrambot.request.EditMessageText;
import com.pengrad.telegrambot.request.SendMessage;
import com.pengrad.telegrambot.request.SetMessageReaction;
import com.pengrad.telegrambot.response.BaseResponse;
import com.pengrad.telegrambot.response.SendResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

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

    public Delivery send(long chatId, String text) {
        return send(chatId, null, text);
    }

    public Delivery sendToTopic(long chatId, int topicId, String text) {
        return send(chatId, topicId, text);
    }

    private Delivery send(long chatId, Integer topicId, String text) {
        if (text == null || text.isBlank()) {
            return Delivery.failed();
        }
        List<Integer> sent = new ArrayList<>();
        for (String chunk : split(text)) {
            SendMessage request = new SendMessage(chatId, chunk);
            if (topicId != null) {
                request.messageThreadId(topicId);
            }
            Integer messageId = execute(chatId, request);
            if (messageId == null) {
                // Report the whole send as failed: a half-delivered message is
                // not something the caller can sensibly act on.
                return Delivery.failed();
            }
            sent.add(messageId);
        }
        return Delivery.of(sent);
    }

    public Delivery sendReply(long chatId, int replyToMessageId, String text) {
        List<String> chunks = split(text);
        List<Integer> sent = new ArrayList<>();
        for (int i = 0; i < chunks.size(); i++) {
            SendMessage request = new SendMessage(chatId, chunks.get(i));
            if (i == 0) {
                request.replyToMessageId(replyToMessageId);
            }
            Integer messageId = execute(chatId, request);
            if (messageId == null) {
                return Delivery.failed();
            }
            sent.add(messageId);
        }
        return Delivery.of(sent);
    }

    /**
     * Rewrites an already-sent message. Used to mirror an edit from one side of
     * the conversation to the other.
     */
    public boolean edit(long chatId, int messageId, String newText, boolean isCaption) {
        try {
            BaseResponse response = isCaption
                    ? telegramBot.execute(new EditMessageCaption(chatId, messageId).caption(newText))
                    : telegramBot.execute(new EditMessageText(chatId, messageId, newText));
            if (!response.isOk()) {
                // "message is not modified" means the text already matches, which
                // is a success as far as the caller is concerned.
                boolean unchanged = response.description() != null
                        && response.description().contains("message is not modified");
                if (!unchanged) {
                    log.warn("Failed to edit message {} in chat {}: {}",
                            messageId, chatId, response.description());
                }
                return unchanged;
            }
            return true;
        } catch (Exception e) {
            log.error("Error editing message {} in chat {}", messageId, chatId, e);
            return false;
        }
    }

    /**
     * Sets reactions on a message. Pass an empty list to remove all reactions.
     *
     * @param chatId   Telegram chat ID
     * @param messageId ID of the message to react to
     * @param reactions reactions to set, or empty list to remove all reactions
     */
    public void setReaction(String chatId, int messageId, List<ReactionType> reactions) {
        try {
            BaseRequest<SetMessageReaction, BaseResponse> request;
            if (reactions == null || reactions.isEmpty()) {
                request = new SetMessageReaction(chatId, messageId);
            } else {
                request = new SetMessageReaction(chatId, messageId, reactions.toArray(new ReactionType[0]));
            }
            BaseResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.error("Failed to set reaction on message {} in chat {}: {} (error {})",
                        messageId, chatId, response.description(), response.errorCode());
            }
        } catch (Exception e) {
            log.error("Error setting reaction on message {} in chat {}", messageId, chatId, e);
        }
    }

    /** @return the sent message ID, or null if the send failed */
    private Integer execute(long chatId, SendMessage request) {
        try {
            SendResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.error("Failed to send message to {}: {}", chatId, response.description());
                return null;
            }
            return response.message() != null ? response.message().messageId() : null;
        } catch (Exception e) {
            log.error("Error sending message to {}", chatId, e);
            return null;
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
