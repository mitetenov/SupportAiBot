package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.Chat;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.User;
import com.vpnsupport.config.TelegramProperties;
import com.vpnsupport.llm.LlmClient;
import com.vpnsupport.llm.LlmProcessingException;
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

import java.util.List;
import java.util.Optional;
import java.util.Set;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class VpnSupportBotUserMessageTest {

    @Mock private TelegramBot telegramBot;
    @Mock private TelegramMessageSender messageSender;
    @Mock private TopicMappingRepository topicMappingRepository;
    @Mock private MessageMappingRepository messageMappingRepository;
    @Mock private SupportGroupForwarder forwarder;
    @Mock private LlmClient llmClient;
    @Mock private FaqEmbeddingService faqEmbeddingService;
    @Mock private UserRateLimiter rateLimiter;
    @Mock private ChatHistoryService chatHistoryService;
    @Mock private WebClient webClient;
    @Mock private LlmTokenUsageRepository tokenUsageRepository;
    @Mock private UserRepository userRepository;
    @Mock private TaskExecutor taskExecutor;

    private VpnSupportBot bot;
    private static final long SUPPORT_CHAT_ID = -100123L;
    private static final long USER_CHAT_ID = 12345L;
    private static final long USER_ID = 111L;

    @BeforeEach
    void setUp() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(SUPPORT_CHAT_ID);
        properties.setSupportAdminTelegramIds("111,222");
        properties.setSupportAdminUsername("admin");

        bot = new VpnSupportBot(
                telegramBot, llmClient, faqEmbeddingService, forwarder,
                topicMappingRepository, messageMappingRepository,
                messageSender, rateLimiter, chatHistoryService,
                webClient, properties, tokenUsageRepository,
                userRepository, taskExecutor);
    }

    private Message userMessage(String text, long userId, long chatId) {
        User user = mock(User.class);
        lenient().when(user.id()).thenReturn(userId);
        lenient().when(user.username()).thenReturn("testuser");
        lenient().when(user.firstName()).thenReturn("Test");

        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(chatId);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.from()).thenReturn(user);
        lenient().when(msg.text()).thenReturn(text);
        lenient().when(msg.messageId()).thenReturn(555);
        return msg;
    }

    private void invokeHandleUserMessage(Message message, String text, String base64Image, String mimeType) {
        ReflectionTestUtils.invokeMethod(bot, "handleUserMessage", message, text, base64Image, mimeType);
    }

    @Test
    void shouldHandleStartCommand() {
        Message msg = userMessage("/start", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);

        invokeHandleUserMessage(msg, "/start", null, null);

        verify(chatHistoryService).clear(USER_ID);
        verify(messageSender).send(eq(USER_CHAT_ID), contains("Привет!"));
        verify(llmClient, never()).chat(anyString(), anyLong());
    }

    @Test
    void shouldHandleOperatorCommand() {
        Message msg = userMessage("/operator", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);

        invokeHandleUserMessage(msg, "/operator", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Передаю ваш запрос оператору"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(), contains("[Запрос оператора]"), eq("Пользователь запросил живого оператора."), eq(true));
    }

    @Test
    void shouldHandleStatsForAdmin() {
        Message msg = userMessage("/stats", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);

        invokeHandleUserMessage(msg, "/stats", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Статистика пока пуста"));
    }

    @Test
    void shouldHandleStatsWithLimitForAdmin() {
        Message msg = userMessage("/stats 5", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(tokenUsageRepository.findTopByTokens(any()))
                .thenReturn(List.of(new TokenStatsDto(111L, 1000, 600, 400, 5)));

        invokeHandleUserMessage(msg, "/stats 5", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Топ-5"));
    }

    @Test
    void shouldSilentlyIgnoreStatsForNonAdmin() {
        Message msg = userMessage("/stats", 999L, USER_CHAT_ID);
        when(userRepository.findById(999L)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);

        invokeHandleUserMessage(msg, "/stats", null, null);

        verify(messageSender, never()).send(anyLong(), anyString());
        verify(rateLimiter, never()).tryAcquire(anyLong());
    }

    @Test
    void shouldHandleUnknownSlashCommand() {
        Message msg = userMessage("/unknown", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);

        invokeHandleUserMessage(msg, "/unknown", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Неизвестная команда"));
    }

    @Test
    void shouldBlockWhenRateLimited() {
        Message msg = userMessage("question", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(false);

        invokeHandleUserMessage(msg, "question", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Подождите несколько секунд"));
        verify(llmClient, never()).chat(anyString(), anyLong());
    }

    @Test
    void shouldProcessTextMessageWithLlm() {
        Message msg = userMessage("Не работает VPN", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(eq("Не работает VPN"), eq(USER_ID)))
                .thenReturn("Попробуйте обновить подписку и пинг");

        invokeHandleUserMessage(msg, "Не работает VPN", null, null);

        verify(llmClient).chat(eq("Не работает VPN"), eq(USER_ID));
        verify(messageSender).send(eq(USER_CHAT_ID), eq("Попробуйте обновить подписку и пинг"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(),
                eq("Не работает VPN"), eq("Попробуйте обновить подписку и пинг"), eq(false));
    }

    @Test
    void shouldDetectEscalationMarker() {
        Message msg = userMessage("Оплатил но не продлилось", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenReturn("Обратитесь в @PeipivoSalesBot [ESCALATE]");

        invokeHandleUserMessage(msg, "Оплатил но не продлилось", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), eq("Обратитесь в @PeipivoSalesBot"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(),
                eq("Оплатил но не продлилось"), eq("Обратитесь в @PeipivoSalesBot"), eq(true));
    }

    @Test
    void shouldDetectHumanRequestKeyword() {
        Message msg = userMessage("позовите оператора", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenReturn("Переключаю на оператора");

        invokeHandleUserMessage(msg, "позовите оператора", null, null);

        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(),
                eq("позовите оператора"), eq("Переключаю на оператора"), eq(true));
    }

    @Test
    void shouldHandleLlmProcessingException() {
        Message msg = userMessage("bad request", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenThrow(new LlmProcessingException("api error", "Пожалуйста, попробуйте позже."));

        invokeHandleUserMessage(msg, "bad request", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), eq("Пожалуйста, попробуйте позже."));
        verify(forwarder).forwardErrorToTopic(any(), eq("bad request"),
                eq("Пожалуйста, попробуйте позже."), anyString());
    }

    @Test
    void shouldHandleGenericException() {
        Message msg = userMessage("crash", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenThrow(new RuntimeException("Something broke"));

        invokeHandleUserMessage(msg, "crash", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Произошла ошибка"));
        verify(forwarder).forwardErrorToTopic(any(), eq("crash"),
                contains("Произошла ошибка"), anyString());
    }

    @Test
    void shouldAttachFaqImagesWhenPolicyAllows() {
        Message msg = userMessage("как настроить VPN", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenReturn("Вот инструкция по настройке VPN...");
        when(faqEmbeddingService.getMatchedImages(anyString()))
                .thenReturn(List.of("img1.jpg", "img2.jpg"));

        invokeHandleUserMessage(msg, "как настроить VPN", null, null);

        verify(messageSender).sendPhoto(eq(USER_CHAT_ID), eq("img1.jpg"));
        verify(messageSender).sendPhoto(eq(USER_CHAT_ID), eq("img2.jpg"));
    }

    @Test
    void shouldNotAttachImagesForPaymentIssues() {
        Message msg = userMessage("у меня закончился трафик", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenReturn("Ваш трафик исчерпан. Обратитесь в @PeipivoSalesBot");

        invokeHandleUserMessage(msg, "у меня закончился трафик", null, null);

        verify(faqEmbeddingService, never()).getMatchedImages(anyString());
        verify(messageSender, never()).sendPhoto(anyLong(), anyString());
    }

    @Test
    void shouldHandleEmptyLlmResponse() {
        Message msg = userMessage("...", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chat(anyString(), eq(USER_ID)))
                .thenReturn("[ESCALATE]");

        invokeHandleUserMessage(msg, "...", null, null);

        verify(messageSender).send(eq(USER_CHAT_ID), contains("Передаю ваш запрос оператору"));
    }

    @Test
    void shouldHandleTextWithScreenshot() {
        Message msg = userMessage("что на скриншоте", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chatWithImage(eq("что на скриншоте"), eq(USER_ID), eq("base64data"), eq("image/png")))
                .thenReturn("На скриншоте видна ошибка подключения");

        invokeHandleUserMessage(msg, "что на скриншоте", "base64data", "image/png");

        verify(llmClient).chatWithImage(eq("что на скриншоте"), eq(USER_ID), eq("base64data"), eq("image/png"));
        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(),
                contains("[Скриншот]"), anyString(), eq(false));
    }

    @Test
    void shouldHandleScreenshotWithoutText() {
        Message msg = userMessage("", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(true);
        when(llmClient.chatWithImage(eq(""), eq(USER_ID), eq("base64data"), eq("image/jpeg")))
                .thenReturn("На скриншоте...");

        invokeHandleUserMessage(msg, "", "base64data", "image/jpeg");

        verify(forwarder).forwardToSupport(eq(USER_CHAT_ID), eq(555), any(),
                eq("[Скриншот]"), anyString(), eq(false));
    }

    @Test
    void shouldSaveUserInfoOnMessage() {
        Message msg = userMessage("hello", USER_ID, USER_CHAT_ID);

        when(userRepository.findById(USER_ID)).thenReturn(Optional.empty());
        when(userRepository.save(any())).thenReturn(null);
        when(rateLimiter.tryAcquire(USER_ID)).thenReturn(false);

        invokeHandleUserMessage(msg, "hello", null, null);

        verify(userRepository).save(argThat(entity ->
                entity.getTelegramId() == USER_ID
                && "testuser".equals(entity.getUsername())
                && "Test".equals(entity.getFirstName())
        ));
    }
}
