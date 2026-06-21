package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.UpdatesListener;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.PhotoSize;
import com.pengrad.telegrambot.model.Update;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.request.GetFile;
import com.pengrad.telegrambot.response.GetFileResponse;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.rag.FaqEmbeddingService;
import com.vpnsupport.support.SupportGroupForwarder;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.Base64;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

@Component
public class VpnSupportBot {

    private static final Logger log = LoggerFactory.getLogger(VpnSupportBot.class);

    private final TelegramBot telegramBot;
    private final LlmClient llmClient;
    private final FaqEmbeddingService faqEmbeddingService;
    private final SupportGroupForwarder forwarder;
    private final TopicMappingRepository topicMappingRepository;
    private final TelegramMessageSender messageSender;
    private final UserRateLimiter rateLimiter;
    private final ChatHistoryService chatHistoryService;
    private final WebClient webClient;
    private final long supportGroupChatId;

    private ExecutorService updateExecutor;

    public VpnSupportBot(TelegramBot telegramBot, LlmClient llmClient,
                          FaqEmbeddingService faqEmbeddingService,
                          SupportGroupForwarder forwarder,
                          TopicMappingRepository topicMappingRepository,
                          TelegramMessageSender messageSender,
                          UserRateLimiter rateLimiter,
                          ChatHistoryService chatHistoryService,
                          WebClient webClient,
                          TelegramProperties telegramProperties) {
        this.telegramBot = telegramBot;
        this.llmClient = llmClient;
        this.faqEmbeddingService = faqEmbeddingService;
        this.forwarder = forwarder;
        this.topicMappingRepository = topicMappingRepository;
        this.messageSender = messageSender;
        this.rateLimiter = rateLimiter;
        this.chatHistoryService = chatHistoryService;
        this.webClient = webClient;
        this.supportGroupChatId = telegramProperties.getSupportGroupChatId();
    }

    @PostConstruct
    public void start() {
        updateExecutor = Executors.newFixedThreadPool(4);
        telegramBot.setUpdatesListener(updates -> {
            for (Update update : updates) {
                updateExecutor.submit(() -> processUpdate(update));
            }
            return UpdatesListener.CONFIRMED_UPDATES_ALL;
        }, e -> log.error("Telegram updates listener error", e));

        log.info("VPN Support Bot started");
    }

    @PreDestroy
    public void stop() {
        telegramBot.removeGetUpdatesListener();
        if (updateExecutor != null) {
            updateExecutor.shutdown();
            try {
                if (!updateExecutor.awaitTermination(30, TimeUnit.SECONDS)) {
                    updateExecutor.shutdownNow();
                }
            } catch (InterruptedException e) {
                updateExecutor.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }
    }

    private void processUpdate(Update update) {
        Message message = update.message();
        if (message == null || message.from() == null) {
            return;
        }
        if (message.from().isBot()) {
            return;
        }

        long chatId = message.chat().id();

        if (chatId == supportGroupChatId) {
            handleSupportGroupMessage(message);
        } else if (message.text() != null && !message.text().isBlank()) {
            handleTextMessage(message);
        } else if (message.photo() != null && message.photo().length > 0) {
            handlePhotoMessage(message);
        }
    }

    private void handleSupportGroupMessage(Message message) {
        Integer topicId = message.messageThreadId();
        if (topicId == null) {
            return;
        }

        topicMappingRepository.findByTopicId(topicId).ifPresentOrElse(mapping -> {
            String text = message.text() != null ? message.text().trim() : "";
            if (!text.isEmpty()) {
                messageSender.send(mapping.getUserId(), "Поддержка: " + text);
            } else {
                copyToUser(mapping.getUserId(), message);
            }
            messageSender.sendReply(supportGroupChatId, message.messageId(),
                    "Отправлено пользователю.");
        }, () -> log.debug("No user mapping found for topic {}", topicId));
    }

    private void copyToUser(long userChatId, Message message) {
        CopyMessage request = new CopyMessage(userChatId, supportGroupChatId, message.messageId());
        try {
            MessageIdResponse response = telegramBot.execute(request);
            if (!response.isOk()) {
                log.warn("Failed to copy support message to user {}: {}", userChatId, response.description());
                messageSender.send(userChatId, "Сообщение от поддержки (не удалось переслать медиа).");
            }
        } catch (Exception e) {
            log.error("Error copying support message to user {}", userChatId, e);
            messageSender.send(userChatId, "Сообщение от поддержки.");
        }
    }

    private void handleTextMessage(Message message) {
        long chatId = message.chat().id();
        User user = message.from();
        String text = message.text().trim();

        if (text.equals("/start")) {
            chatHistoryService.clear(user.id());
            messageSender.send(chatId, "Привет! Я бот технической поддержки VPN-сервиса. "
                    + "Задайте ваш вопрос, и я постараюсь помочь.\n\n"
                    + "Я могу проверить состояние серверов, статистику трафика "
                    + "и помочь с настройкой подключения.\n\n"
                    + "Чтобы связаться с оператором, напишите /operator");
            return;
        }

        if (text.equals("/operator")) {
            messageSender.send(chatId, "Передаю ваш запрос оператору. Ожидайте ответа в этом чате.");
            forwarder.forwardToSupport(chatId, message.messageId(), user,
                    "[Запрос оператора] " + text, "Пользователь запросил живого оператора.", true);
            return;
        }

        if (text.startsWith("/")) {
            messageSender.send(chatId, "Неизвестная команда. Напишите вопрос или /operator для связи с оператором.");
            return;
        }

        if (!rateLimiter.tryAcquire(user.id())) {
            messageSender.send(chatId, "Подождите несколько секунд перед следующим сообщением.");
            return;
        }

        try {
            String response = llmClient.chat(text, user.id());
            messageSender.send(chatId, response);

            if (!isErrorResponse(response) && FaqImagePolicy.shouldAttachImages(response)) {
                List<String> images = faqEmbeddingService.getMatchedImages(text);
                for (String fileId : images) {
                    messageSender.sendPhoto(chatId, fileId);
                }
            }

            boolean escalation = needsEscalation(text, response);
            forwarder.forwardToSupport(chatId, message.messageId(), user, text, response, escalation);
        } catch (Exception e) {
            log.error("Error processing message from user {}", chatId, e);
            messageSender.send(chatId, "Произошла ошибка при обработке запроса. Попробуйте позже.");
        }
    }

    private void handlePhotoMessage(Message message) {
        long chatId = message.chat().id();
        User user = message.from();
        String caption = message.caption() != null ? message.caption().trim() : "";

        if (!llmClient.supportsImages()) {
            messageSender.send(chatId, "Пока что я не умею работать с медиафайлами. Опишите проблему текстом.");
            return;
        }

        if (!rateLimiter.tryAcquire(user.id())) {
            messageSender.send(chatId, "Подождите несколько секунд перед следующим сообщением.");
            return;
        }

        try {
            PhotoSize[] photos = message.photo();
            PhotoSize largestPhoto = photos[photos.length - 1];

            GetFile getFile = new GetFile(largestPhoto.fileId());
            GetFileResponse fileResponse = telegramBot.execute(getFile);

            if (!fileResponse.isOk() || fileResponse.file() == null) {
                messageSender.send(chatId, "Не удалось загрузить изображение. Попробуйте ещё раз.");
                return;
            }

            String fileUrl = telegramBot.getFullFilePath(fileResponse.file());
            String filePath = fileResponse.file().filePath();

            byte[] imageBytes = webClient.get()
                    .uri(fileUrl)
                    .retrieve()
                    .bodyToMono(byte[].class)
                    .block();

            if (imageBytes == null) {
                messageSender.send(chatId, "Не удалось скачать изображение.");
                return;
            }

            String base64Image = Base64.getEncoder().encodeToString(imageBytes);
            String mimeType = detectMimeType(filePath);

            String userPrompt = caption.isEmpty()
                    ? "Посмотри на скриншот. Опиши, что на нём отображается, и помоги решить проблему."
                    : caption;

            String response = llmClient.chatWithImage(userPrompt, user.id(), base64Image, mimeType);

            messageSender.send(chatId, response);

            String forwardText = caption.isEmpty() ? "[Скриншот]" : "[Скриншот] " + caption;
            boolean escalation = needsEscalation(caption, response);
            forwarder.forwardToSupport(chatId, message.messageId(), user, forwardText, response, escalation);
        } catch (Exception e) {
            log.error("Error processing photo from user {}", chatId, e);
            messageSender.send(chatId, "Произошла ошибка при обработке изображения. Попробуйте позже.");
        }
    }

    private boolean isErrorResponse(String response) {
        return response.startsWith("Превышено")
                || response.startsWith("Не удалось")
                || response.startsWith("Произошла ошибка")
                || response.startsWith("Модель не вернула");
    }

    private boolean needsEscalation(String userMessage, String botResponse) {
        String lowerMsg = userMessage.toLowerCase();
        String lowerResp = botResponse.toLowerCase();

        if (lowerMsg.contains("отмен") || lowerMsg.contains("подписк")
                || lowerMsg.contains("верни") || lowerMsg.contains("возврат")
                || lowerMsg.contains("рефанд") || lowerMsg.contains("refund")
                || lowerMsg.contains("жалоб") || lowerMsg.contains("оператор")
                || lowerMsg.contains("человек") || lowerMsg.contains("жив")) {
            return true;
        }

        if (lowerResp.contains("не удалось") || lowerResp.contains("ошибк")
                || lowerResp.contains("не найден") || lowerResp.contains("попробуйте позже")
                || lowerResp.contains("обратитесь")) {
            return true;
        }

        return false;
    }

    private String detectMimeType(String filePath) {
        if (filePath == null) {
            return "image/jpeg";
        }
        String lower = filePath.toLowerCase();
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".webp")) return "image/webp";
        if (lower.endsWith(".gif")) return "image/gif";
        return "image/jpeg";
    }
}
