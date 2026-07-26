package com.vpnsupport.bot;

import com.pengrad.telegrambot.model.Chat;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.User;
import com.vpnsupport.bot.UserMessageBuffer.MessageBatch;
import com.vpnsupport.config.ConversationProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.llm.LlmProcessingException;
import com.vpnsupport.llm.LlmReply;
import com.vpnsupport.rag.FaqEmbeddingService;
import com.vpnsupport.support.SupportGroupForwarder;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class UserMessagePipelineTest {

    private static final long USER_ID = 100L;
    private static final long CHAT_ID = 100L;

    @Mock private LlmClient llmClient;
    @Mock private TelegramMessageSender messageSender;
    @Mock private SupportGroupForwarder forwarder;
    @Mock private UserRateLimiter rateLimiter;
    @Mock private KnowledgeGapService knowledgeGapService;
    @Mock private TypingIndicator typingIndicator;
    @Mock private BotMessages messages;

    private ConversationState conversationState;
    private UserMessagePipeline pipeline;

    @BeforeEach
    void setUp() {
        conversationState = new ConversationState(new ConversationProperties());
        lenient().when(messages.get(anyString())).thenAnswer(inv -> inv.getArgument(0));
        lenient().when(typingIndicator.start(anyLong())).thenReturn(() -> { });

        pipeline = new UserMessagePipeline(llmClient, messageSender, forwarder, rateLimiter,
                knowledgeGapService, conversationState, typingIndicator, messages);
    }

    private MessageBatch batch(String text, List<Integer> messageIds) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(CHAT_ID);

        User user = mock(User.class);
        lenient().when(user.id()).thenReturn(USER_ID);

        Message message = mock(Message.class);
        lenient().when(message.chat()).thenReturn(chat);
        lenient().when(message.from()).thenReturn(user);

        return new MessageBatch(message, user, text, messageIds, null, null);
    }

    private MessageBatch batch(String text) {
        return batch(text, List.of(111));
    }

    @Test
    void shouldAnswerWithTheModel() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat("Не работает VPN", USER_ID)).thenReturn(new LlmReply("Попробуйте пинг"));

        pipeline.handle(batch("Не работает VPN"));

        verify(messageSender).send(CHAT_ID, "Попробуйте пинг");
        verify(forwarder).forwardToSupport(eq(CHAT_ID), eq(List.of(111)), any(),
                eq("Попробуйте пинг"), eq(false));
    }

    @Test
    void shouldStripTheEscalateMarkerAndTagAnAdmin() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong()))
                .thenReturn(new LlmReply("Оформим возврат. [ESCALATE]"));

        pipeline.handle(batch("хочу возврат"));

        verify(messageSender).send(CHAT_ID, "Оформим возврат.");
        verify(forwarder).forwardToSupport(eq(CHAT_ID), any(), any(), eq("Оформим возврат."), eq(true));
    }

    @Test
    void shouldEscalateWhenUserAsksForAHuman() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Чем помочь?"));

        pipeline.handle(batch("позовите оператора"));

        verify(forwarder).forwardToSupport(eq(CHAT_ID), any(), any(), anyString(), eq(true));
    }

    @Test
    void shouldNotEscalateOnAWordThatMerelyContainsATrigger() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Понял"));

        // "живу" contains "жив", which the old substring check escalated on.
        pipeline.handle(batch("я живу в Германии, какой сервер выбрать"));

        verify(forwarder).forwardToSupport(eq(CHAT_ID), any(), any(), anyString(), eq(false));
    }

    @Test
    void shouldForwardEveryMessageOfTheBatch() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Ответ"));

        pipeline.handle(batch("привет\nне работает\nчто делать", List.of(11, 12, 13)));

        verify(forwarder).forwardToSupport(eq(CHAT_ID), eq(List.of(11, 12, 13)), any(),
                eq("Ответ"), eq(false));
    }

    @Test
    void shouldSubstituteAFallbackWhenTheModelReturnsNothing() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("[ESCALATE]"));

        pipeline.handle(batch("вопрос"));

        verify(messageSender).send(CHAT_ID, "bot.llm.empty");
    }

    @Test
    void shouldForwardRatherThanDropAMessageThatHitTheRateLimit() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(false);

        pipeline.handle(batch("седьмое сообщение подряд"));

        verify(messageSender).send(CHAT_ID, "bot.ratelimit.wait");
        // The message must still reach a human rather than vanish.
        verify(forwarder).forwardToSupport(eq(CHAT_ID), eq(List.of(111)), any(),
                eq("support.ratelimited"), eq(true));
        verify(llmClient, never()).chat(anyString(), anyLong());
    }

    @Test
    void shouldStayQuietWhileAnOperatorIsHandlingTheConversation() {
        conversationState.recordOperatorReply(USER_ID);

        pipeline.handle(batch("ещё вопрос"));

        verify(forwarder).forwardToSupport(eq(CHAT_ID), any(), any(),
                eq("support.ai.suppressed"), eq(false));
        verify(llmClient, never()).chat(anyString(), anyLong());
    }

    @Test
    void shouldAnswerAgainOnceTheSuppressionWindowHasPassed() {
        ConversationProperties shortWindow = new ConversationProperties();
        shortWindow.setOperatorSuppressionWindow(java.time.Duration.ZERO);
        ConversationState state = new ConversationState(shortWindow);
        state.recordOperatorReply(USER_ID);

        UserMessagePipeline p = new UserMessagePipeline(llmClient, messageSender, forwarder,
                rateLimiter, knowledgeGapService, state, typingIndicator, messages);

        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Ответ"));

        p.handle(batch("ещё вопрос"));

        verify(llmClient).chat(anyString(), anyLong());
    }

    @Test
    void shouldPassTheRetrievalToKnowledgeGapAccounting() {
        FaqEmbeddingService.FaqContext context = new FaqEmbeddingService.FaqContext(
                "FAQ...", List.of(new FaqEmbeddingService.FaqResult("Q", "A", 0.44, 0.01)), 0.44, "Q");

        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Ответ", context));

        pipeline.handle(batch("странный вопрос"));

        verify(knowledgeGapService).evaluate(eq("странный вопрос"), eq(USER_ID), eq("Ответ"), eq(context));
    }

    @Test
    void shouldRememberTheLastQueryForOperatorAttribution() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenReturn(new LlmReply("Ответ"));

        pipeline.handle(batch("мой вопрос"));

        org.junit.jupiter.api.Assertions.assertEquals("мой вопрос",
                conversationState.lastQuery(USER_ID).orElseThrow().text());
    }

    @Test
    void shouldReportModelFailuresToTheUserAndTheTopic() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong()))
                .thenThrow(new LlmProcessingException("boom", "Модель недоступна"));

        pipeline.handle(batch("вопрос"));

        verify(messageSender).send(CHAT_ID, "Модель недоступна");
        verify(forwarder).forwardErrorToTopic(any(), eq("вопрос"), eq("Модель недоступна"), anyString());
    }

    @Test
    void shouldReportUnexpectedFailuresWithTheGenericMessage() {
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), anyLong())).thenThrow(new RuntimeException("db down"));

        pipeline.handle(batch("вопрос"));

        verify(messageSender).send(CHAT_ID, "bot.llm.error");
        verify(forwarder).forwardErrorToTopic(any(), eq("вопрос"), eq("bot.llm.error"), anyString());
    }

    @Test
    void shouldUseTheVisionCallWhenTheBatchCarriesAnImage() {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(CHAT_ID);
        User user = mock(User.class);
        lenient().when(user.id()).thenReturn(USER_ID);
        Message message = mock(Message.class);
        lenient().when(message.chat()).thenReturn(chat);

        MessageBatch withImage = new MessageBatch(message, user, "что тут не так",
                List.of(111), "BASE64", "image/png");

        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chatWithImage("что тут не так", USER_ID, "BASE64", "image/png"))
                .thenReturn(new LlmReply("Вижу ошибку подписки"));

        pipeline.handle(withImage);

        verify(messageSender).send(CHAT_ID, "Вижу ошибку подписки");
        verify(llmClient, never()).chat(anyString(), anyLong());
    }
}
