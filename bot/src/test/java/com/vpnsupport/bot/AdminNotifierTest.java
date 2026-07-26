package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.request.SendMessage;
import com.pengrad.telegrambot.response.SendResponse;
import com.vpnsupport.config.TelegramProperties;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class AdminNotifierTest {

    private static final long SUPPORT_CHAT_ID = -100123L;

    @Mock private TelegramBot telegramBot;

    private AdminNotifier notifier;

    @BeforeEach
    void setUp() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(SUPPORT_CHAT_ID);
        notifier = new AdminNotifier(telegramBot, properties);

        SendResponse ok = mock(SendResponse.class);
        lenient().when(ok.isOk()).thenReturn(true);
        lenient().when(telegramBot.execute(any(SendMessage.class))).thenReturn(ok);
    }

    private SendMessage captureSent() {
        ArgumentCaptor<SendMessage> captor = ArgumentCaptor.forClass(SendMessage.class);
        org.mockito.Mockito.verify(telegramBot).execute(captor.capture());
        return captor.getValue();
    }

    private String sentText() {
        return String.valueOf(captureSent().getParameters().get("text"));
    }

    @Test
    void shouldSendTheErrorToTheSupportGroup() {
        notifier.notifyError("MCP init failed", new IllegalStateException("connection refused"));

        SendMessage sent = captureSent();
        assertEquals(SUPPORT_CHAT_ID, sent.getParameters().get("chat_id"));
        String text = String.valueOf(sent.getParameters().get("text"));
        assertTrue(text.contains("MCP init failed"));
        assertTrue(text.contains("connection refused"));
    }

    @Test
    void shouldIncludeTheUserWhenOneIsGiven() {
        notifier.notifyError("Tool call failed", 4242L, new RuntimeException("boom"));

        assertTrue(sentText().contains("4242"));
    }

    @Test
    void shouldOmitTheUserLineWhenThereIsNone() {
        notifier.notifyError("Startup failed", new RuntimeException("boom"));

        assertFalse(sentText().contains("User:"));
    }

    /**
     * Telegram rejects messages over 4096 characters, so a stack-trace-sized
     * exception message must be cut before it is sent — otherwise the error
     * report itself fails to deliver.
     */
    @Test
    void shouldTruncateAnOverlongErrorMessage() {
        notifier.notifyError("Boom", new RuntimeException("X".repeat(10_000)));

        assertTrue(sentText().length() < 4096,
                "the notification must fit in a single Telegram message");
    }

    @Test
    void shouldCopeWithAnExceptionThatHasNoMessage() {
        assertDoesNotThrow(() -> notifier.notifyError("Boom", new RuntimeException()));

        assertTrue(sentText().contains("Boom"));
    }

    @Test
    void shouldSendSilentlySoTheGroupIsNotWokenUp() {
        notifier.notifyError("Boom", new RuntimeException("x"));

        assertEquals(true, captureSent().getParameters().get("disable_notification"));
    }

    @Test
    void shouldNotPropagateAFailureToSendTheNotification() {
        when(telegramBot.execute(any(SendMessage.class)))
                .thenThrow(new RuntimeException("Telegram unreachable"));

        // Notification is best-effort: it must never mask the original error.
        assertDoesNotThrow(() -> notifier.notifyError("Boom", new RuntimeException("original")));
    }

    @Test
    void shouldNotThrowWhenTelegramRejectsTheNotification() {
        SendResponse rejected = mock(SendResponse.class);
        when(rejected.isOk()).thenReturn(false);
        when(rejected.description()).thenReturn("chat not found");
        when(telegramBot.execute(any(SendMessage.class))).thenReturn(rejected);

        assertDoesNotThrow(() -> notifier.notifyError("Boom", new RuntimeException("original")));
    }
}
