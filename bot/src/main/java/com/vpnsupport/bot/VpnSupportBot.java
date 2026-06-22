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
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.Base64;
import java.util.List;
import java.util.Set;
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
    private final LlmTokenUsageRepository tokenUsageRepository;
    private final Set<Long> adminTelegramIds;
    private final JdbcTemplate jdbcTemplate;

    private ExecutorService updateExecutor;

    public VpnSupportBot(TelegramBot telegramBot, LlmClient llmClient,
                          FaqEmbeddingService faqEmbeddingService,
                          SupportGroupForwarder forwarder,
                          TopicMappingRepository topicMappingRepository,
                          TelegramMessageSender messageSender,
                          UserRateLimiter rateLimiter,
                          ChatHistoryService chatHistoryService,
                          WebClient webClient,
                          TelegramProperties telegramProperties,
                          LlmTokenUsageRepository tokenUsageRepository,
                          JdbcTemplate jdbcTemplate) {
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
        this.tokenUsageRepository = tokenUsageRepository;
        this.adminTelegramIds = telegramProperties.getSupportAdminTelegramIds();
        this.jdbcTemplate = jdbcTemplate;
    }

    @EventListener(ApplicationReadyEvent.class)
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

        ensureUserInfo(user);

        if (text.equals("/start")) {
            chatHistoryService.clear(user.id());
            messageSender.send(chatId, "Привет! Я бот технической поддержки VPN-сервиса.\n\n"
                    + "Что я умею:\n"
                    + "• Диагностика подключения и проверка состояния серверов\n"
                    + "• Управление устройствами — список, удаление\n"
                    + "• Сброс подписки с выдачей новой ссылки\n"
                    + "• Проверка трафика и статистика\n"
                    + "• Ответы на частые вопросы\n\n"
                    + "Просто напишите ваш вопрос.\n"
                    + "Оператор: /operator");
            return;
        }

        if (text.equals("/operator")) {
            messageSender.send(chatId, "Передаю ваш запрос оператору. Ожидайте ответа в этом чате.");
            forwarder.forwardToSupport(chatId, message.messageId(), user,
                    "[Запрос оператора] " + text, "Пользователь запросил живого оператора.", true);
            return;
        }

        if (text.startsWith("/stats") && adminTelegramIds.contains(user.id())) {
            handleStats(chatId, text);
            return;
        }

        if (text.startsWith("/stats") || text.startsWith("/")) {
            if (text.startsWith("/stats")) {
                return; // silently ignore for non-admins
            }
            messageSender.send(chatId, "Неизвестная команда. Напишите вопрос или /operator для связи с оператором.");
            return;
        }

        if (!rateLimiter.tryAcquire(user.id())) {
            messageSender.send(chatId, "Подождите несколько секунд перед следующим сообщением.");
            return;
        }

        try {
            String rawResponse = llmClient.chat(text, user.id());
            String response = stripEscalateMarker(rawResponse);
            if (response.isEmpty()) {
                response = "Передаю ваш запрос оператору. Ожидайте ответа в этом чате.";
            }

            messageSender.send(chatId, response);

            if (!isErrorResponse(response) && FaqImagePolicy.shouldAttachImages(response)) {
                List<String> images = faqEmbeddingService.getMatchedImages(text);
                for (String fileId : images) {
                    messageSender.sendPhoto(chatId, fileId);
                }
            }

            boolean escalation = llmRequestedEscalation(rawResponse)
                    || isErrorResponse(response)
                    || userRequestsHuman(text);
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

            String rawResponse = llmClient.chatWithImage(userPrompt, user.id(), base64Image, mimeType);
            String response = stripEscalateMarker(rawResponse);
            if (response.isEmpty()) {
                response = "Передаю ваш запрос оператору. Ожидайте ответа в этом чате.";
            }

            messageSender.send(chatId, response);

            String forwardText = caption.isEmpty() ? "[Скриншот]" : "[Скриншот] " + caption;
            boolean escalation = llmRequestedEscalation(rawResponse)
                    || isErrorResponse(response)
                    || userRequestsHuman(caption);
            forwarder.forwardToSupport(chatId, message.messageId(), user, forwardText, response, escalation);
        } catch (Exception e) {
            log.error("Error processing photo from user {}", chatId, e);
            messageSender.send(chatId, "Произошла ошибка при обработке изображения. Попробуйте позже.");
        }
    }

    private static final String ESCALATE_MARKER = "[ESCALATE]";

    private void handleStats(long chatId, String command) {
        String[] parts = command.split("\\s+");
        if (parts.length == 2) {
            try {
                long num = Long.parseLong(parts[1]);
                if (num <= 100) {
                    showTopStats(chatId, (int) Math.clamp(num, 1, 100));
                } else {
                    showUserStats(chatId, num);
                }
                return;
            } catch (NumberFormatException ignored) {
            }
        }
        showTopStats(chatId, 10);
    }

    private void showTopStats(long chatId, int limit) {
        List<Object[]> top = tokenUsageRepository.findTopByTokens(
                org.springframework.data.domain.PageRequest.of(0, limit));
        if (top.isEmpty()) {
            messageSender.send(chatId, "Статистика пока пуста.");
            return;
        }
        StringBuilder sb = new StringBuilder("Топ-").append(limit)
                .append(" пользователей по токенам LLM:\n");
        int rank = 1;
        for (Object[] row : top) {
            Long tgId = (Long) row[0];
            sb.append(rank++).append(". ").append(resolveUserName(tgId))
                    .append(" — ").append(formatNumber((Long) row[1]))
                    .append(" токенов (").append(row[4]).append(" запросов)\n");
        }
        messageSender.send(chatId, sb.toString());
    }

    private void showUserStats(long chatId, long telegramId) {
        List<Object[]> stats = tokenUsageRepository.getStatsByTelegramId(telegramId);
        if (stats.isEmpty() || stats.get(0)[0] == null) {
            messageSender.send(chatId, "Нет данных по " + resolveUserName(telegramId) + ".");
            return;
        }
        Object[] row = stats.get(0);
        messageSender.send(chatId,
                "Статистика " + resolveUserName(telegramId) + ":\n"
                        + "Запросов: " + row[3] + "\n"
                        + "Prompt-токенов: " + formatNumber((Long) row[1]) + "\n"
                        + "Completion-токенов: " + formatNumber((Long) row[2]) + "\n"
                        + "Всего токенов: " + formatNumber((Long) row[0]));
    }

    private static String formatNumber(long n) {
        if (n < 1_000) return String.valueOf(n);
        if (n < 1_000_000) return String.format("%.1fK", n / 1_000.0);
        if (n < 1_000_000_000) return String.format("%.1fM", n / 1_000_000.0);
        return String.format("%.1fB", n / 1_000_000_000.0);
    }

    private void ensureUserInfo(User user) {
        try {
            jdbcTemplate.execute("""
                    CREATE TABLE IF NOT EXISTS user_names (
                        telegram_id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        last_name VARCHAR(255),
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """);
            jdbcTemplate.update(
                    "INSERT INTO user_names (telegram_id, username, first_name, last_name, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                            + "ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username, "
                            + "first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, "
                            + "updated_at = CURRENT_TIMESTAMP",
                    user.id(),
                    user.username(),
                    user.firstName(),
                    user.lastName());
        } catch (Exception e) {
            log.warn("Failed to save user info: {}", e.getMessage());
        }
    }

    private String resolveUserName(Long telegramId) {
        try {
            List<java.util.Map<String, Object>> rows = jdbcTemplate.queryForList(
                    "SELECT username FROM user_names WHERE telegram_id = ?", telegramId);
            if (!rows.isEmpty()) {
                String username = (String) rows.get(0).get("username");
                if (username != null && !username.isBlank()) {
                    return "@" + username + " (" + telegramId + ")";
                }
            }
        } catch (Exception e) {
            log.warn("Failed to resolve user name: {}", e.getMessage());
        }
        return String.valueOf(telegramId);
    }

    private boolean isErrorResponse(String response) {
        return response.startsWith("Превышено")
                || response.startsWith("Не удалось")
                || response.startsWith("Произошла ошибка")
                || response.startsWith("Модель не вернула");
    }

    private String stripEscalateMarker(String rawResponse) {
        if (rawResponse == null) {
            return null;
        }
        return rawResponse.replace(ESCALATE_MARKER, "").trim();
    }

    private boolean llmRequestedEscalation(String rawResponse) {
        return rawResponse != null && rawResponse.contains(ESCALATE_MARKER);
    }

    private boolean userRequestsHuman(String userMessage) {
        if (userMessage == null || userMessage.isBlank()) {
            return false;
        }
        String lower = userMessage.toLowerCase();
        return lower.contains("оператор")
                || lower.contains("человек")
                || lower.contains("жив");
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
