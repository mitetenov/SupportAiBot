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
import com.pengrad.telegrambot.request.SendChatAction;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.rag.FaqEmbeddingService;
import com.vpnsupport.support.SupportGroupForwarder;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.core.task.TaskExecutor;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.LocalDateTime;
import java.util.Base64;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class VpnSupportBot {

    private static final Logger log = LoggerFactory.getLogger(VpnSupportBot.class);
    private static final String ESCALATE_MARKER = "[ESCALATE]";

    private final TelegramBot telegramBot;
    private final LlmClient llmClient;
    private final FaqEmbeddingService faqEmbeddingService;
    private final SupportGroupForwarder forwarder;
    private final TopicMappingRepository topicMappingRepository;
    private final MessageMappingRepository messageMappingRepository;
    private final TelegramMessageSender messageSender;
    private final UserRateLimiter rateLimiter;
    private final ChatHistoryService chatHistoryService;
    private final WebClient webClient;
    private final long supportGroupChatId;
    private final LlmTokenUsageRepository tokenUsageRepository;
    private final Set<Long> adminTelegramIds;
    private final UserRepository userRepository;
    private final TaskExecutor taskExecutor;
    private final ConcurrentHashMap<Long, Long> lastOperatorReply = new ConcurrentHashMap<>();

    public VpnSupportBot(TelegramBot telegramBot, LlmClient llmClient,
                          FaqEmbeddingService faqEmbeddingService,
                          SupportGroupForwarder forwarder,
                          TopicMappingRepository topicMappingRepository,
                          MessageMappingRepository messageMappingRepository,
                          TelegramMessageSender messageSender,
                          UserRateLimiter rateLimiter,
                          ChatHistoryService chatHistoryService,
                          WebClient webClient,
                          TelegramProperties telegramProperties,
                          LlmTokenUsageRepository tokenUsageRepository,
                          UserRepository userRepository,
                          TaskExecutor taskExecutor) {
        this.telegramBot = telegramBot;
        this.llmClient = llmClient;
        this.faqEmbeddingService = faqEmbeddingService;
        this.forwarder = forwarder;
        this.topicMappingRepository = topicMappingRepository;
        this.messageMappingRepository = messageMappingRepository;
        this.messageSender = messageSender;
        this.rateLimiter = rateLimiter;
        this.chatHistoryService = chatHistoryService;
        this.webClient = webClient;
        this.supportGroupChatId = telegramProperties.getSupportGroupChatId();
        this.tokenUsageRepository = tokenUsageRepository;
        this.adminTelegramIds = telegramProperties.getSupportAdminTelegramIds();
        this.userRepository = userRepository;
        this.taskExecutor = taskExecutor;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void start() {
        telegramBot.setUpdatesListener(updates -> {
            for (Update update : updates) {
                taskExecutor.execute(() -> processUpdate(update));
            }
            return UpdatesListener.CONFIRMED_UPDATES_ALL;
        }, e -> log.error("Telegram updates listener error", e));

        log.info("VPN Support Bot started");
    }

    @PreDestroy
    public void stop() {
        telegramBot.removeGetUpdatesListener();
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
            handleUserMessage(message, message.text().trim(), null, null);
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
                // Check if the operator replied to a specific message in the topic
                Message repliedTo = message.replyToMessage();
                if (repliedTo != null) {
                    Integer repliedToMessageId = repliedTo.messageId();
                    // Look up the original user message that maps to this topic message
                    messageMappingRepository
                            .findByTopicMessageIdAndTopicId(repliedToMessageId, topicId)
                            .ifPresentOrElse(msgMapping -> {
                                // Send as a reply to the original user message
                                messageSender.sendReply(
                                        msgMapping.getUserChatId(),
                                        msgMapping.getUserMessageId(),
                                        text);
                            }, () -> {
                                // Fallback: no mapping found, send as new message
                                messageSender.send(mapping.getUserId(), "Поддержка: " + text);
                            });
                } else {
                    // No reply — send as a new message as before
                    messageSender.send(mapping.getUserId(), "Поддержка: " + text);
                }
            } else {
                copyToUser(mapping.getUserId(), message);
            }
            messageSender.sendReply(supportGroupChatId, message.messageId(),
                    "Отправлено пользователю.");
            lastOperatorReply.put(mapping.getUserId(), System.currentTimeMillis());
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

    private void handleUserMessage(Message message, String text, String base64Image, String mimeType) {
        long chatId = message.chat().id();
        User user = message.from();

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
                    + "Просто напишите ваш вопрос.");
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

        // Suppress AI replies for 30 minutes after the latest operator reply
        Long lastOpReply = lastOperatorReply.get(chatId);
        if (lastOpReply != null && System.currentTimeMillis() - lastOpReply < 30 * 60 * 1000L) {
            forwarder.forwardToSupport(chatId, message.messageId(), user,
                    text, "[AI suppressed — operator recently active]", false);
            return;
        }

        try {
            telegramBot.execute(new SendChatAction(chatId, "typing"));
            String rawResponse;
            if (base64Image != null) {
                rawResponse = llmClient.chatWithImage(text, user.id(), base64Image, mimeType);
            } else {
                rawResponse = llmClient.chat(text, user.id());
            }

            String response = stripEscalateMarker(rawResponse);
            if (response.isEmpty()) {
                response = "Передаю ваш запрос оператору. Ожидайте ответа в этом чате.";
            }

            messageSender.send(chatId, response);

            boolean llmEscalation = llmRequestedEscalation(rawResponse);
            boolean humanRequest = userRequestsHuman(text);
            
            String forwardText = text;
            if (base64Image != null) {
                forwardText = text.isEmpty() ? "[Скриншот]" : "[Скриншот] " + text;
            }

            if (base64Image == null && FaqImagePolicy.shouldAttachImages(response)) {
                List<String> images = faqEmbeddingService.getMatchedImages(text);
                for (String fileId : images) {
                    messageSender.sendPhoto(chatId, fileId);
                }
            }
            forwarder.forwardToSupport(chatId, message.messageId(), user, forwardText, response,
                    llmEscalation || humanRequest);
        } catch (com.vpnsupport.llm.LlmProcessingException e) {
            log.error("LLM error processing message from user {}", chatId, e);
            messageSender.send(chatId, e.getUserFriendlyMessage());
            String forwardText = (base64Image != null && text.isEmpty()) ? "[Скриншот]" : 
                                 (base64Image != null ? "[Скриншот] " + text : text);
            forwarder.forwardErrorToTopic(user, forwardText, e.getUserFriendlyMessage(), extractErrorMessage(e));
        } catch (Exception e) {
            log.error("Error processing message from user {}", chatId, e);
            String errorText = "Произошла ошибка при обработке запроса. Попробуйте позже.";
            messageSender.send(chatId, errorText);
            String forwardText = (base64Image != null && text.isEmpty()) ? "[Скриншот]" : 
                                 (base64Image != null ? "[Скриншот] " + text : text);
            forwarder.forwardErrorToTopic(user, forwardText, errorText, extractErrorMessage(e));
        }
    }

    private void handlePhotoMessage(Message message) {
        long chatId = message.chat().id();
        String caption = message.caption() != null ? message.caption().trim() : "";

        if (!llmClient.supportsImages()) {
            messageSender.send(chatId, "Пока что я не умею работать с медиафайлами. Опишите проблему текстом.");
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

            handleUserMessage(message, userPrompt, base64Image, mimeType);
        } catch (Exception e) {
            log.error("Error downloading photo from user {}", chatId, e);
            messageSender.send(chatId, "Произошла ошибка при загрузке изображения. Попробуйте позже.");
        }
    }

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
            UserEntity entity = userRepository.findById(user.id()).orElse(new UserEntity());
            entity.setTelegramId(user.id());
            entity.setUsername(user.username());
            entity.setFirstName(user.firstName());
            entity.setLastName(user.lastName());
            entity.setUpdatedAt(LocalDateTime.now());
            userRepository.save(entity);
        } catch (Exception e) {
            log.warn("Failed to save user info: {}", e.getMessage());
        }
    }

    private String resolveUserName(Long telegramId) {
        try {
            return userRepository.findById(telegramId)
                    .map(u -> {
                        if (u.getUsername() != null && !u.getUsername().isBlank()) {
                            return "@" + u.getUsername() + " (" + telegramId + ")";
                        }
                        return String.valueOf(telegramId);
                    })
                    .orElse(String.valueOf(telegramId));
        } catch (Exception e) {
            log.warn("Failed to resolve user name: {}", e.getMessage());
        }
        return String.valueOf(telegramId);
    }

    private String stripEscalateMarker(String rawResponse) {
        if (rawResponse == null) {
            return "";
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

    private String extractErrorMessage(Exception e) {
        String msg = e.getMessage();
        if (msg != null && msg.length() > 3000) {
            msg = msg.substring(0, 3000) + "...";
        }
        return "Bot: " + (msg != null ? msg : e.getClass().getSimpleName());
    }
}
