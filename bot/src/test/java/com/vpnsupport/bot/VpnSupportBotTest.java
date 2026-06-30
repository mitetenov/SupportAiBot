package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.Chat;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.rag.FaqEmbeddingService;
import com.vpnsupport.support.SupportGroupForwarder;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.task.TaskExecutor;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class VpnSupportBotTest {

    @Mock
    private TelegramBot telegramBot;

    @Mock
    private TelegramMessageSender messageSender;

    @Mock
    private TopicMappingRepository topicMappingRepository;

    @Mock
    private MessageMappingRepository messageMappingRepository;

    @Mock
    private SupportGroupForwarder forwarder;

    @Mock
    private LlmClient llmClient;

    @Mock
    private FaqEmbeddingService faqEmbeddingService;

    @Mock
    private UserRateLimiter rateLimiter;

    @Mock
    private ChatHistoryService chatHistoryService;

    @Mock
    private WebClient webClient;

    @Mock
    private LlmTokenUsageRepository tokenUsageRepository;

    @Mock
    private UserRepository userRepository;

    @Mock
    private TaskExecutor taskExecutor;

    private VpnSupportBot bot;

    private static final long SUPPORT_CHAT_ID = -100123L;

    @BeforeEach
    void setUp() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(SUPPORT_CHAT_ID);
        properties.setSupportAdminTelegramIds("");

        bot = new VpnSupportBot(
                telegramBot, llmClient, faqEmbeddingService, forwarder,
                topicMappingRepository, messageMappingRepository,
                messageSender, rateLimiter, chatHistoryService,
                webClient, properties, tokenUsageRepository,
                userRepository, taskExecutor);
    }

    /**
     * Helper: invoke the private handleSupportGroupMessage method via reflection.
     */
    private void invokeHandleSupportGroupMessage(Message message) {
        ReflectionTestUtils.invokeMethod(bot, "handleSupportGroupMessage", message);
    }

    /**
     * Helper: create a mock Message from the support group with given parameters.
     */
    @SuppressWarnings("unchecked")
    private Message supportGroupMessage(Integer topicId, String text,
                                         Message replyToMessage, Integer messageId) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(SUPPORT_CHAT_ID);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.messageThreadId()).thenReturn(topicId);
        lenient().when(msg.text()).thenReturn(text);
        lenient().when(msg.messageId()).thenReturn(messageId != null ? messageId : 999);
        lenient().when(msg.replyToMessage()).thenReturn(replyToMessage);
        return msg;
    }

    /**
     * Helper: create a TopicMapping for a given user.
     */
    private TopicMapping topicMapping(Long userId, Integer topicId) {
        return new TopicMapping(userId, topicId, "testuser");
    }

    /**
     * Helper: create a MessageMapping for a given topic message → user message.
     */
    private MessageMapping messageMapping(Integer topicMessageId, Integer topicId,
                                           Long userChatId, Integer userMessageId) {
        return new MessageMapping(topicMessageId, topicId, userChatId, userMessageId);
    }

    // ───── Tests for handleSupportGroupMessage ─────

    @Test
    void shouldSendNewMessageWhenOperatorDoesNotReply() {
        // Operator writes a new message (not a reply) in a topic
        Integer topicId = 42;
        Long userId = 100L;
        String text = "Ваш вопрос решён.";
        Message message = supportGroupMessage(topicId, text, null, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));

        invokeHandleSupportGroupMessage(message);

        verify(messageSender).send(userId, "Поддержка: " + text);
        verify(messageSender).sendReply(SUPPORT_CHAT_ID, 900, "Отправлено пользователю.");
        verifyNoInteractions(messageMappingRepository);
    }

    @Test
    void shouldSendReplyWhenOperatorRepliesToUserMessage() {
        // Operator replies to a user's message in the topic
        Integer topicId = 42;
        Long userId = 100L;
        int userMessageId = 555;
        int repliedTopicMessageId = 777;
        String replyText = "Вот инструкция...";

        Message repliedToMessage = mock(Message.class);
        when(repliedToMessage.messageId()).thenReturn(repliedTopicMessageId);

        Message message = supportGroupMessage(topicId, replyText, repliedToMessage, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));
        when(messageMappingRepository.findByTopicMessageIdAndTopicId(repliedTopicMessageId, topicId))
                .thenReturn(Optional.of(messageMapping(repliedTopicMessageId, topicId, userId, userMessageId)));

        invokeHandleSupportGroupMessage(message);

        // Should send as reply to the original user message, without the "Поддержка: " prefix
        verify(messageSender).sendReply(userId, userMessageId, replyText);
        verify(messageSender).sendReply(SUPPORT_CHAT_ID, 900, "Отправлено пользователю.");
        verify(messageSender, never()).send(eq(userId), anyString());
    }

    @Test
    void shouldFallbackToNewMessageWhenOperatorRepliesButMappingNotFound() {
        // Operator replies, but the replied-to message has no mapping saved
        Integer topicId = 42;
        Long userId = 100L;
        int repliedTopicMessageId = 777;
        String replyText = "Текст ответа";

        Message repliedToMessage = mock(Message.class);
        when(repliedToMessage.messageId()).thenReturn(repliedTopicMessageId);

        Message message = supportGroupMessage(topicId, replyText, repliedToMessage, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));
        when(messageMappingRepository.findByTopicMessageIdAndTopicId(repliedTopicMessageId, topicId))
                .thenReturn(Optional.empty());

        invokeHandleSupportGroupMessage(message);

        // Should fall back to sending as a new message with the prefix
        verify(messageSender).send(userId, "Поддержка: " + replyText);
        verify(messageSender, never()).sendReply(eq(userId), anyInt(), anyString());
    }

    @Test
    void shouldIgnoreMessageWhenTopicIdIsNull() {
        Message message = supportGroupMessage(null, "text", null, 900);

        invokeHandleSupportGroupMessage(message);

        verifyNoInteractions(topicMappingRepository, messageMappingRepository, messageSender);
    }

    @Test
    void shouldCopyMediaWhenTextIsEmpty() {
        // Message with no text (e.g. photo) — should use copyToUser
        Integer topicId = 42;
        Long userId = 100L;
        Message message = supportGroupMessage(topicId, "", null, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));

        // Make copyToUser succeed so it doesn't fall back to send()
        MessageIdResponse okResponse = mock(MessageIdResponse.class);
        when(okResponse.isOk()).thenReturn(true);
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        invokeHandleSupportGroupMessage(message);

        verify(telegramBot).execute(any(CopyMessage.class));
        verify(messageSender, never()).send(anyLong(), anyString());
        verify(messageSender).sendReply(SUPPORT_CHAT_ID, 900, "Отправлено пользователю.");
    }

    @Test
    void shouldCopyMediaWhenOperatorRepliesWithEmptyText() {
        // Operator replies to a message but with empty text (media-only)
        Integer topicId = 42;
        Long userId = 100L;

        Message message = supportGroupMessage(topicId, "", null, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));

        // Make copyToUser succeed
        MessageIdResponse okResponse = mock(MessageIdResponse.class);
        when(okResponse.isOk()).thenReturn(true);
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        invokeHandleSupportGroupMessage(message);

        // Empty text should take the copyToUser path regardless of replyToMessage
        verify(telegramBot).execute(any(CopyMessage.class));
        verify(messageSender, never()).send(anyLong(), anyString());
        // Confirmation reply should still be sent back to the topic
        verify(messageSender).sendReply(SUPPORT_CHAT_ID, 900, "Отправлено пользователю.");
    }

    // ───── Tests for lastOperatorReply recording ─────

    @Test
    @SuppressWarnings("unchecked")
    void shouldRecordTimestampWhenOperatorSendsNewMessage() {
        Integer topicId = 42;
        Long userId = 100L;
        Message message = supportGroupMessage(topicId, "Решено.", null, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));

        invokeHandleSupportGroupMessage(message);

        ConcurrentHashMap<Long, Long> map = (ConcurrentHashMap<Long, Long>)
                ReflectionTestUtils.getField(bot, "lastOperatorReply");
        assert map != null;
        Long timestamp = map.get(userId);
        assert timestamp != null : "Timestamp should be recorded for userId";
        assert Math.abs(System.currentTimeMillis() - timestamp) < 5000
                : "Timestamp should be within 5 seconds of now";
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldRecordTimestampWhenOperatorRepliesToMessage() {
        Integer topicId = 42;
        Long userId = 100L;
        int repliedTopicMessageId = 777;

        Message repliedToMessage = mock(Message.class);
        when(repliedToMessage.messageId()).thenReturn(repliedTopicMessageId);

        Message message = supportGroupMessage(topicId, "Вот инструкция.", repliedToMessage, 900);

        when(topicMappingRepository.findByTopicId(topicId))
                .thenReturn(Optional.of(topicMapping(userId, topicId)));
        when(messageMappingRepository.findByTopicMessageIdAndTopicId(repliedTopicMessageId, topicId))
                .thenReturn(Optional.of(messageMapping(repliedTopicMessageId, topicId, userId, 555)));

        invokeHandleSupportGroupMessage(message);

        ConcurrentHashMap<Long, Long> map = (ConcurrentHashMap<Long, Long>)
                ReflectionTestUtils.getField(bot, "lastOperatorReply");
        assert map != null;
        Long timestamp = map.get(userId);
        assert timestamp != null : "Timestamp should be recorded for userId";
        assert Math.abs(System.currentTimeMillis() - timestamp) < 5000
                : "Timestamp should be within 5 seconds of now";
    }

    // ───── Tests for AI suppression in handleUserMessage ─────

    /**
     * Helper: create a mock Message from a user DM with given parameters.
     */
    private Message userMessage(long chatId, long userId, String text, Integer messageId) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(chatId);

        User from = mock(User.class);
        lenient().when(from.id()).thenReturn(userId);
        lenient().when(from.username()).thenReturn("testuser");
        lenient().when(from.firstName()).thenReturn("Test");
        lenient().when(from.lastName()).thenReturn("User");
        lenient().when(from.isBot()).thenReturn(false);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.from()).thenReturn(from);
        lenient().when(msg.messageId()).thenReturn(messageId != null ? messageId : 111);
        lenient().when(msg.text()).thenReturn(text);
        return msg;
    }

    /**
     * Helper: invoke the private handleUserMessage method via reflection.
     */
    private void invokeHandleUserMessage(Message message, String text) {
        ReflectionTestUtils.invokeMethod(bot, "handleUserMessage", message, text, null, null);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldSuppressAiReplyWhenOperatorRepliedRecently() {
        long chatId = 100L;
        long userId = 100L;
        String text = "Есть вопрос";
        Message message = userMessage(chatId, userId, text, 111);

        when(rateLimiter.tryAcquire(userId)).thenReturn(true);
        // Pretend the user was seen before
        when(userRepository.findById(userId)).thenReturn(Optional.empty());

        // Inject a recent operator reply timestamp (10 seconds ago)
        ConcurrentHashMap<Long, Long> map = (ConcurrentHashMap<Long, Long>)
                ReflectionTestUtils.getField(bot, "lastOperatorReply");
        assert map != null;
        map.put(chatId, System.currentTimeMillis() - 10_000);

        invokeHandleUserMessage(message, text);

        // Should forward to support without AI reply
        verify(forwarder).forwardToSupport(eq(chatId), eq(111), any(), eq(text),
                eq("[AI suppressed — operator recently active]"), eq(false));
        verify(llmClient, never()).chat(anyString(), anyLong());
        verify(llmClient, never()).chatWithImage(anyString(), anyLong(), anyString(), anyString());
    }

    @Test
    void shouldNotSuppressWhenNoOperatorReplyRecorded() {
        long chatId = 100L;
        long userId = 100L;
        String text = "Есть вопрос";
        Message message = userMessage(chatId, userId, text, 111);

        when(rateLimiter.tryAcquire(userId)).thenReturn(true);
        when(userRepository.findById(userId)).thenReturn(Optional.empty());
        when(llmClient.chat(text, userId)).thenReturn("Ответ бота");

        invokeHandleUserMessage(message, text);

        // AI reply should be generated
        verify(llmClient).chat(text, userId);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldNotSuppressWhenOperatorReplyIsOlderThan30Minutes() {
        long chatId = 100L;
        long userId = 100L;
        String text = "Есть вопрос";
        Message message = userMessage(chatId, userId, text, 111);

        when(rateLimiter.tryAcquire(userId)).thenReturn(true);
        when(userRepository.findById(userId)).thenReturn(Optional.empty());
        when(llmClient.chat(text, userId)).thenReturn("Ответ бота");

        // Inject a stale operator reply timestamp (35 minutes ago)
        ConcurrentHashMap<Long, Long> map = (ConcurrentHashMap<Long, Long>)
                ReflectionTestUtils.getField(bot, "lastOperatorReply");
        assert map != null;
        map.put(chatId, System.currentTimeMillis() - 35 * 60 * 1000L);

        invokeHandleUserMessage(message, text);

        // AI reply should still be generated
        verify(llmClient).chat(text, userId);
    }

    @Test
    void shouldNotSuppressForStartCommand() {
        long chatId = 100L;
        long userId = 100L;
        String text = "/start";
        Message message = userMessage(chatId, userId, text, 111);

        // Even with a recent operator reply, /start should not trigger suppression
        // because it returns before the suppression check

        invokeHandleUserMessage(message, text);

        verify(chatHistoryService).clear(userId);
        verify(messageSender).send(eq(chatId), anyString());
        verifyNoInteractions(rateLimiter);
        verify(llmClient, never()).chat(anyString(), anyLong());
    }
}
