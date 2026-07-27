package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.UpdatesListener;
import com.pengrad.telegrambot.model.BotCommand;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.MessageReactionUpdated;
import com.pengrad.telegrambot.model.Update;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.model.reaction.ReactionType;
import com.pengrad.telegrambot.model.reaction.ReactionTypeEmoji;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.request.GetUpdates;
import com.pengrad.telegrambot.request.SetMyCommands;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.bot.UserMessageBuffer.BufferedMessage;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.support.SupportGroupForwarder;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.core.task.TaskExecutor;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Arrays;
import java.util.List;

/**
 * Routes Telegram updates.
 *
 * <p>Commands, photo downloading, message batching and the answer pipeline each
 * live in their own component; what remains here is deciding where an update
 * goes.
 */
@Component
public class VpnSupportBot {

    private static final Logger log = LoggerFactory.getLogger(VpnSupportBot.class);
    private static final String DELIVERED_REACTION = "👍";

    private final TelegramBot telegramBot;
    private final LlmClient llmClient;
    private final SupportGroupForwarder forwarder;
    private final TopicMappingRepository topicMappingRepository;
    private final MessageMappingRepository messageMappingRepository;
    private final TelegramMessageSender messageSender;
    private final ChatHistoryService chatHistoryService;
    private final KnowledgeGapService knowledgeGapService;
    private final SupportCommandHandler commandHandler;
    private final PhotoDownloader photoDownloader;
    private final UserMessageBuffer messageBuffer;
    private final UserMessagePipeline pipeline;
    private final ConversationState conversationState;
    private final BotMessages messages;
    private final UserRepository userRepository;
    private final TaskExecutor taskExecutor;
    private final long supportGroupChatId;

    public VpnSupportBot(TelegramBot telegramBot, LlmClient llmClient,
                         SupportGroupForwarder forwarder,
                         TopicMappingRepository topicMappingRepository,
                         MessageMappingRepository messageMappingRepository,
                         TelegramMessageSender messageSender,
                         ChatHistoryService chatHistoryService,
                         KnowledgeGapService knowledgeGapService,
                         SupportCommandHandler commandHandler,
                         PhotoDownloader photoDownloader,
                         UserMessageBuffer messageBuffer,
                         UserMessagePipeline pipeline,
                         ConversationState conversationState,
                         BotMessages messages,
                         UserRepository userRepository,
                         TaskExecutor taskExecutor,
                         TelegramProperties telegramProperties) {
        this.telegramBot = telegramBot;
        this.llmClient = llmClient;
        this.forwarder = forwarder;
        this.topicMappingRepository = topicMappingRepository;
        this.messageMappingRepository = messageMappingRepository;
        this.messageSender = messageSender;
        this.chatHistoryService = chatHistoryService;
        this.knowledgeGapService = knowledgeGapService;
        this.commandHandler = commandHandler;
        this.photoDownloader = photoDownloader;
        this.messageBuffer = messageBuffer;
        this.pipeline = pipeline;
        this.conversationState = conversationState;
        this.messages = messages;
        this.userRepository = userRepository;
        this.taskExecutor = taskExecutor;
        this.supportGroupChatId = telegramProperties.getSupportGroupChatId();
    }

    @EventListener(ApplicationReadyEvent.class)
    public void start() {
        publishCommandMenu();

        GetUpdates getUpdates = new GetUpdates().allowedUpdates("message", "message_reaction");
        telegramBot.setUpdatesListener(updates -> {
            for (Update update : updates) {
                taskExecutor.execute(() -> processUpdate(update));
            }
            return UpdatesListener.CONFIRMED_UPDATES_ALL;
        }, e -> log.error("Telegram updates listener error", e), getUpdates);

        log.info("VPN Support Bot started");
    }

    /** Registers the command list so Telegram clients can offer it in the UI. */
    private void publishCommandMenu() {
        try {
            telegramBot.execute(new SetMyCommands(
                    new BotCommand("start", "Начать заново, сбросить историю"),
                    new BotCommand("operator", "Связаться с живым оператором"),
                    new BotCommand("help", "Что умеет бот")));
        } catch (Exception e) {
            log.warn("Failed to publish the command menu: {}", e.getMessage());
        }
    }

    @PreDestroy
    public void stop() {
        telegramBot.removeGetUpdatesListener();
    }

    void processUpdate(Update update) {
        MessageReactionUpdated reaction = update.messageReaction();
        if (reaction != null) {
            handleReactionUpdated(reaction);
            return;
        }

        Message message = update.message();
        // isBot() is a boxed Boolean and may be null; unboxing it directly would NPE.
        if (message == null || message.from() == null || Boolean.TRUE.equals(message.from().isBot())) {
            return;
        }

        if (message.chat().id() == supportGroupChatId) {
            handleSupportGroupMessage(message);
            return;
        }

        routeUserMessage(message);
    }

    // ---------------------------------------------------------------- user side

    private void routeUserMessage(Message message) {
        long chatId = message.chat().id();
        User user = message.from();
        ensureUserInfo(user);

        String text = message.text() != null ? message.text().trim() : null;

        if (text != null && !text.isEmpty()) {
            if (commandHandler.isCommand(text)) {
                handleCommand(message, chatId, user, text);
            } else {
                buffer(user.id(), BufferedMessage.text(message, text));
            }
            return;
        }

        if (message.photo() != null && message.photo().length > 0) {
            handlePhoto(message, chatId, user);
            return;
        }

        // Voice notes, video, documents, stickers. These used to fall through to
        // nothing at all: the user got silence and no way to tell whether the bot
        // was broken or ignoring them.
        handleUnsupportedMedia(message, chatId, user);
    }

    private void handleCommand(Message message, long chatId, User user, String text) {
        switch (text.split("\\s+")[0]) {
            case "/start" -> {
                chatHistoryService.clear(user.id());
                conversationState.clear(user.id());
                messageSender.send(chatId, messages.get("bot.start.welcome"));
            }
            case "/help" -> commandHandler.sendHelp(chatId);
            case "/operator" -> handleOperatorRequest(message, chatId, user);
            default -> {
                if (!commandHandler.handleAdminCommand(chatId, user.id(), text)) {
                    commandHandler.sendUnknownCommand(chatId);
                }
            }
        }
    }

    private void handleOperatorRequest(Message message, long chatId, User user) {
        conversationState.lastQuery(user.id()).ifPresent(last ->
                knowledgeGapService.evaluateOperatorRequest(
                        last.text(), user.id(), last.faqContextOrEmpty()));

        messageSender.send(chatId, messages.get("bot.operator.transfer"));
        forwarder.forwardToSupport(chatId, List.of(message.messageId()), user,
                messages.get("support.operator.request"), true);
    }

    private void handlePhoto(Message message, long chatId, User user) {
        if (!llmClient.supportsImages()) {
            messageSender.send(chatId, messages.get("bot.photo.notsupported"));
            forwarder.forwardToSupport(chatId, List.of(message.messageId()), user,
                    messages.get("support.media.received"), false);
            return;
        }

        PhotoDownloader.Result result = photoDownloader.download(message.photo());
        if (!result.isSuccess()) {
            messageSender.send(chatId, messages.get(result.errorMessageKey()));
            return;
        }

        String caption = message.caption() != null ? message.caption().trim() : "";
        String prompt = caption.isEmpty() ? messages.get("bot.photo.default.prompt") : caption;

        buffer(user.id(), new BufferedMessage(message, prompt, result.base64Image(), result.mimeType()));
    }

    private void handleUnsupportedMedia(Message message, long chatId, User user) {
        messageSender.send(chatId, messages.get("bot.media.unsupported"));
        forwarder.forwardToSupport(chatId, List.of(message.messageId()), user,
                messages.get("support.media.received"), false);
    }

    private void buffer(long userId, BufferedMessage message) {
        messageBuffer.submit(userId, message, batch -> taskExecutor.execute(() -> pipeline.handle(batch)));
    }

    // ------------------------------------------------------------- support side

    private void handleSupportGroupMessage(Message message) {
        Integer topicId = message.messageThreadId();
        if (topicId == null) {
            return;
        }

        topicMappingRepository.findByTopicId(topicId).ifPresentOrElse(mapping -> {
            String text = message.text() != null ? message.text().trim() : "";
            if (text.isEmpty()) {
                copyToUser(mapping.getUserId(), message);
            } else {
                deliverOperatorText(message, topicId, mapping.getUserId(), text);
            }
            // A 👍 on the operator's own message instead of a reply: the topic
            // used to be half-filled with "Отправлено пользователю."
            acknowledgeDelivery(message.messageId());
            conversationState.recordOperatorReply(mapping.getUserId());
        }, () -> log.debug("No user mapping found for topic {}", topicId));
    }

    private void deliverOperatorText(Message message, Integer topicId, long userId, String text) {
        Message repliedTo = message.replyToMessage();
        if (repliedTo == null) {
            messageSender.send(userId, messages.get("support.operator.prefix", text));
            return;
        }

        messageMappingRepository.findByTopicMessageIdAndTopicId(repliedTo.messageId(), topicId)
                .ifPresentOrElse(
                        msgMapping -> messageSender.sendReply(
                                msgMapping.getUserChatId(), msgMapping.getUserMessageId(), text),
                        () -> messageSender.send(userId, messages.get("support.operator.prefix", text)));
    }

    private void acknowledgeDelivery(int operatorMessageId) {
        messageSender.setReaction(String.valueOf(supportGroupChatId), operatorMessageId,
                List.of(new ReactionTypeEmoji(DELIVERED_REACTION)));
    }

    private void copyToUser(long userChatId, Message message) {
        try {
            MessageIdResponse response = telegramBot.execute(
                    new CopyMessage(userChatId, supportGroupChatId, message.messageId()));
            if (!response.isOk()) {
                log.warn("Failed to copy support message to user {}: {}", userChatId, response.description());
                messageSender.send(userChatId, messages.get("support.fallback.media"));
            }
        } catch (Exception e) {
            log.error("Error copying support message to user {}", userChatId, e);
            messageSender.send(userChatId, messages.get("support.fallback"));
        }
    }

    private void handleReactionUpdated(MessageReactionUpdated reaction) {
        long chatId = reaction.chat().id();
        int messageId = reaction.messageId();
        ReactionType[] newReactions = reaction.newReaction();
        List<ReactionType> reactions = newReactions != null ? Arrays.asList(newReactions) : List.of();

        if (chatId == supportGroupChatId) {
            messageMappingRepository.findByTopicMessageId(messageId).ifPresent(mapping ->
                    messageSender.setReaction(String.valueOf(mapping.getUserChatId()),
                            mapping.getUserMessageId(), reactions));
        } else {
            messageMappingRepository.findByUserChatIdAndUserMessageId(String.valueOf(chatId), messageId)
                    .ifPresent(mapping -> messageSender.setReaction(String.valueOf(supportGroupChatId),
                            mapping.getTopicMessageId(), reactions));
        }
    }

    // ------------------------------------------------------------------ helpers

    private void ensureUserInfo(User user) {
        try {
            UserEntity entity = userRepository.findById(user.id()).orElseGet(UserEntity::new);
            entity.setTelegramId(user.id());
            entity.setUsername(user.username());
            entity.setFirstName(user.firstName());
            entity.setLastName(user.lastName());
            entity.setUpdatedAt(LocalDateTime.now(ZoneOffset.UTC));
            userRepository.save(entity);
        } catch (Exception e) {
            log.warn("Failed to save user info: {}", e.getMessage());
        }
    }
}
