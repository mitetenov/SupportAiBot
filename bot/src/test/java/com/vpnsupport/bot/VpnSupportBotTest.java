package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.Chat;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.MessageReactionUpdated;
import com.pengrad.telegrambot.model.PhotoSize;
import com.pengrad.telegrambot.model.Update;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.model.reaction.ReactionType;
import com.pengrad.telegrambot.model.reaction.ReactionTypeEmoji;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.config.MessageBufferProperties;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.support.SupportGroupForwarder;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.task.TaskExecutor;

import java.time.Duration;
import java.util.List;
import java.util.Optional;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * Covers update routing: which component an incoming update is handed to.
 * The answer pipeline itself is exercised in {@link UserMessagePipelineTest}.
 */
@ExtendWith(MockitoExtension.class)
class VpnSupportBotTest {

    private static final long SUPPORT_CHAT_ID = -100123L;
    private static final long USER_CHAT_ID = 100L;

    @Mock private TelegramBot telegramBot;
    @Mock private TelegramMessageSender messageSender;
    @Mock private TopicMappingRepository topicMappingRepository;
    @Mock private MessageMappingRepository messageMappingRepository;
    @Mock private SupportGroupForwarder forwarder;
    @Mock private LlmClient llmClient;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private KnowledgeGapService knowledgeGapService;
    @Mock private SupportCommandHandler commandHandler;
    @Mock private PhotoDownloader photoDownloader;
    @Mock private UserMessagePipeline pipeline;
    @Mock private BotMessages messages;
    @Mock private UserRepository userRepository;

    private UserMessageBuffer messageBuffer;
    private ConversationState conversationState;
    private VpnSupportBot bot;

    @BeforeEach
    void setUp() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(SUPPORT_CHAT_ID);
        properties.setSupportAdminTelegramIds("");

        MessageBufferProperties bufferProperties = new MessageBufferProperties();
        bufferProperties.setWindow(Duration.ofMillis(20));
        messageBuffer = new UserMessageBuffer(bufferProperties);
        conversationState = new ConversationState(new com.vpnsupport.config.ConversationProperties());

        // Run submitted work inline so routing assertions stay deterministic.
        TaskExecutor inlineExecutor = Runnable::run;

        lenient().when(messages.get(anyString())).thenAnswer(inv -> inv.getArgument(0));
        lenient().when(messages.get(anyString(), any())).thenAnswer(inv -> inv.getArgument(0));

        bot = new VpnSupportBot(
                telegramBot, llmClient, forwarder, topicMappingRepository,
                messageMappingRepository, messageSender, chatHistoryService,
                knowledgeGapService, commandHandler, photoDownloader,
                messageBuffer, pipeline, conversationState, messages,
                userRepository, inlineExecutor, properties);
    }

    // ------------------------------------------------------------- test helpers

    private Message userMessage(String text, Integer messageId) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(USER_CHAT_ID);

        User from = mock(User.class);
        lenient().when(from.id()).thenReturn(USER_CHAT_ID);
        lenient().when(from.username()).thenReturn("testuser");
        lenient().when(from.isBot()).thenReturn(false);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.from()).thenReturn(from);
        lenient().when(msg.messageId()).thenReturn(messageId != null ? messageId : 111);
        lenient().when(msg.text()).thenReturn(text);
        return msg;
    }

    private Update update(Message message) {
        Update update = mock(Update.class);
        lenient().when(update.message()).thenReturn(message);
        lenient().when(update.messageReaction()).thenReturn(null);
        return update;
    }

    private Message supportGroupMessage(Integer topicId, String text,
                                        Message replyToMessage, Integer messageId) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(SUPPORT_CHAT_ID);

        User from = mock(User.class);
        lenient().when(from.isBot()).thenReturn(false);
        lenient().when(from.id()).thenReturn(7L);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.from()).thenReturn(from);
        lenient().when(msg.messageThreadId()).thenReturn(topicId);
        lenient().when(msg.text()).thenReturn(text);
        lenient().when(msg.messageId()).thenReturn(messageId != null ? messageId : 999);
        lenient().when(msg.replyToMessage()).thenReturn(replyToMessage);
        return msg;
    }

    private TopicMapping topicMapping(Long userId, Integer topicId) {
        return new TopicMapping(userId, topicId, "testuser");
    }

    private MessageMapping messageMapping(Integer topicMessageId, Integer topicId,
                                          Long userChatId, Integer userMessageId) {
        return new MessageMapping(topicMessageId, topicId, userChatId, userMessageId);
    }

    // ------------------------------------------------------- unsupported media

    @Test
    void shouldAnswerAndForwardWhenUserSendsUnsupportedMedia() {
        // A voice note: no text, no photo. This used to produce total silence.
        Message voice = userMessage(null, 222);
        when(voice.photo()).thenReturn(null);

        bot.processUpdate(update(voice));

        verify(messageSender).send(eq(USER_CHAT_ID), eq("bot.media.unsupported"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(List.of(222)), any(),
                eq("support.media.received"), eq(false));
    }

    @Test
    void shouldNotSendUnsupportedMediaNoticeForPlainText() {
        bot.processUpdate(update(userMessage("Не работает VPN", 111)));

        verify(messageSender, never()).send(anyLong(), eq("bot.media.unsupported"));
    }

    @Test
    void shouldTellUserWhenProviderCannotSeeImages() {
        Message photo = userMessage(null, 333);
        when(photo.photo()).thenReturn(new PhotoSize[]{mock(PhotoSize.class)});
        when(llmClient.supportsImages()).thenReturn(false);

        bot.processUpdate(update(photo));

        verify(messageSender).send(eq(USER_CHAT_ID), eq("bot.photo.notsupported"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(List.of(333)), any(),
                eq("support.media.received"), eq(false));
        verifyNoInteractions(photoDownloader);
    }

    @Test
    void shouldReportDownloadFailureToTheUser() {
        Message photo = userMessage(null, 333);
        when(photo.photo()).thenReturn(new PhotoSize[]{mock(PhotoSize.class)});
        when(llmClient.supportsImages()).thenReturn(true);
        when(photoDownloader.download(any()))
                .thenReturn(new PhotoDownloader.Result(null, null, "bot.photo.download.error"));

        bot.processUpdate(update(photo));

        verify(messageSender).send(eq(USER_CHAT_ID), eq("bot.photo.download.error"));
    }

    // ------------------------------------------------------------------ commands

    @Test
    void shouldClearHistoryOnStart() {
        when(commandHandler.isCommand("/start")).thenReturn(true);

        bot.processUpdate(update(userMessage("/start", 111)));

        verify(chatHistoryService).clear(USER_CHAT_ID);
        verify(messageSender).send(eq(USER_CHAT_ID), eq("bot.start.welcome"));
        verifyNoInteractions(pipeline);
    }

    @Test
    void shouldSendHelp() {
        when(commandHandler.isCommand("/help")).thenReturn(true);

        bot.processUpdate(update(userMessage("/help", 111)));

        verify(commandHandler).sendHelp(USER_CHAT_ID);
    }

    @Test
    void shouldEscalateOnOperatorCommand() {
        when(commandHandler.isCommand("/operator")).thenReturn(true);

        bot.processUpdate(update(userMessage("/operator", 111)));

        verify(messageSender).send(eq(USER_CHAT_ID), eq("bot.operator.transfer"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(List.of(111)), any(),
                eq("support.operator.request"), eq(true));
    }

    @Test
    void shouldFallBackToUnknownCommandWhenNotAnAdminCommand() {
        when(commandHandler.isCommand("/whatever")).thenReturn(true);
        when(commandHandler.handleAdminCommand(anyLong(), anyLong(), eq("/whatever"))).thenReturn(false);

        bot.processUpdate(update(userMessage("/whatever", 111)));

        verify(commandHandler).sendUnknownCommand(USER_CHAT_ID);
    }

    @Test
    void shouldNotAnswerUnknownCommandWhenAdminCommandHandledIt() {
        when(commandHandler.isCommand("/stats")).thenReturn(true);
        when(commandHandler.handleAdminCommand(anyLong(), anyLong(), eq("/stats"))).thenReturn(true);

        bot.processUpdate(update(userMessage("/stats", 111)));

        verify(commandHandler, never()).sendUnknownCommand(anyLong());
    }

    @Test
    void shouldNotRouteCommandsIntoTheAnswerPipeline() {
        when(commandHandler.isCommand("/start")).thenReturn(true);

        bot.processUpdate(update(userMessage("/start", 111)));

        verifyNoInteractions(pipeline);
    }

    // -------------------------------------------------------------- support side

    @Test
    void shouldSendNewMessageWhenOperatorDoesNotReply() {
        Message message = supportGroupMessage(42, "Ваш вопрос решён.", null, 900);
        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));

        bot.processUpdate(update(message));

        verify(messageSender).send(eq(100L), eq("support.operator.prefix"));
    }

    @Test
    void shouldAcknowledgeOperatorMessageWithAReactionNotAReply() {
        Message message = supportGroupMessage(42, "Готово", null, 900);
        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));

        bot.processUpdate(update(message));

        verify(messageSender).setReaction(eq(String.valueOf(SUPPORT_CHAT_ID)), eq(900),
                eq(List.of(new ReactionTypeEmoji("👍"))));
        verify(messageSender, never()).sendReply(eq(SUPPORT_CHAT_ID), anyInt(), anyString());
    }

    @Test
    void shouldSendReplyWhenOperatorRepliesToUserMessage() {
        Message repliedTo = mock(Message.class);
        when(repliedTo.messageId()).thenReturn(777);
        Message message = supportGroupMessage(42, "Вот инструкция...", repliedTo, 900);

        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));
        when(messageMappingRepository.findByTopicMessageIdAndTopicId(777, 42))
                .thenReturn(Optional.of(messageMapping(777, 42, 100L, 555)));

        bot.processUpdate(update(message));

        verify(messageSender).sendReply(100L, 555, "Вот инструкция...");
        verify(messageSender, never()).send(eq(100L), anyString());
    }

    @Test
    void shouldFallbackToNewMessageWhenReplyMappingIsMissing() {
        Message repliedTo = mock(Message.class);
        when(repliedTo.messageId()).thenReturn(777);
        Message message = supportGroupMessage(42, "Текст ответа", repliedTo, 900);

        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));
        when(messageMappingRepository.findByTopicMessageIdAndTopicId(777, 42)).thenReturn(Optional.empty());

        bot.processUpdate(update(message));

        verify(messageSender).send(eq(100L), eq("support.operator.prefix"));
        verify(messageSender, never()).sendReply(eq(100L), anyInt(), anyString());
    }

    @Test
    void shouldIgnoreSupportMessageWithoutTopic() {
        bot.processUpdate(update(supportGroupMessage(null, "text", null, 900)));

        verifyNoInteractions(topicMappingRepository, messageMappingRepository);
    }

    @Test
    void shouldCopyMediaWhenOperatorSendsNoText() {
        Message message = supportGroupMessage(42, "", null, 900);
        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));

        MessageIdResponse okResponse = mock(MessageIdResponse.class);
        when(okResponse.isOk()).thenReturn(true);
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        bot.processUpdate(update(message));

        verify(telegramBot).execute(any(CopyMessage.class));
        verify(messageSender, never()).send(anyLong(), anyString());
    }

    @Test
    void shouldRecordOperatorActivityOnReply() {
        Message message = supportGroupMessage(42, "Решено.", null, 900);
        when(topicMappingRepository.findByTopicId(42)).thenReturn(Optional.of(topicMapping(100L, 42)));

        bot.processUpdate(update(message));

        org.junit.jupiter.api.Assertions.assertTrue(conversationState.isOperatorRecentlyActive(100L));
    }

    // ----------------------------------------------------------------- reactions

    private MessageReactionUpdated reactionUpdated(long chatId, int messageId, ReactionType[] newReactions) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(chatId);

        MessageReactionUpdated reaction = mock(MessageReactionUpdated.class);
        lenient().when(reaction.chat()).thenReturn(chat);
        lenient().when(reaction.messageId()).thenReturn(messageId);
        lenient().when(reaction.newReaction()).thenReturn(newReactions);
        return reaction;
    }

    private Update reactionUpdate(MessageReactionUpdated reaction) {
        Update update = mock(Update.class);
        lenient().when(update.messageReaction()).thenReturn(reaction);
        return update;
    }

    @Test
    void shouldForwardReactionFromSupportGroupToUser() {
        ReactionTypeEmoji thumbsUp = new ReactionTypeEmoji("👍");
        when(messageMappingRepository.findByTopicMessageId(300))
                .thenReturn(Optional.of(messageMapping(300, 200, 100L, 42)));

        bot.processUpdate(reactionUpdate(
                reactionUpdated(SUPPORT_CHAT_ID, 300, new ReactionType[]{thumbsUp})));

        verify(messageSender).setReaction(eq("100"), eq(42), eq(List.of(thumbsUp)));
    }

    @Test
    void shouldForwardReactionFromUserToSupportGroup() {
        ReactionTypeEmoji heart = new ReactionTypeEmoji("❤️");
        when(messageMappingRepository.findByUserChatIdAndUserMessageId("100", 42))
                .thenReturn(Optional.of(messageMapping(300, 200, 100L, 42)));

        bot.processUpdate(reactionUpdate(
                reactionUpdated(100L, 42, new ReactionType[]{heart})));

        verify(messageSender).setReaction(eq(String.valueOf(SUPPORT_CHAT_ID)), eq(300), eq(List.of(heart)));
    }

    @Test
    void shouldClearReactionsWhenUserRemovesThem() {
        when(messageMappingRepository.findByUserChatIdAndUserMessageId("100", 42))
                .thenReturn(Optional.of(messageMapping(300, 200, 100L, 42)));

        bot.processUpdate(reactionUpdate(reactionUpdated(100L, 42, new ReactionType[]{})));

        verify(messageSender).setReaction(eq(String.valueOf(SUPPORT_CHAT_ID)), eq(300), eq(List.of()));
    }

    @Test
    void shouldIgnoreMessagesFromBots() {
        Message message = userMessage("hi", 111);
        when(message.from().isBot()).thenReturn(true);

        bot.processUpdate(update(message));

        verifyNoInteractions(pipeline, messageSender);
    }
}
