package com.vpnsupport.support;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.User;
import com.pengrad.telegrambot.request.CopyMessage;
import com.pengrad.telegrambot.response.MessageIdResponse;
import com.vpnsupport.bot.TelegramMessageSender;
import com.vpnsupport.bot.TopicManager;
import com.vpnsupport.config.TelegramProperties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class SupportGroupForwarderTest {

    @Mock
    private TelegramBot telegramBot;

    @Mock
    private TelegramMessageSender messageSender;

    @Mock
    private TopicManager topicManager;

    @Test
    void shouldForwardToSupportWithExistingTopic() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(telegramBot, atLeastOnce()).execute(any(CopyMessage.class));
        verify(messageSender).sendToTopic(eq(100L), eq(42), anyString());
    }

    @Test
    void shouldNotForwardWhenTopicIsNull() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(null);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(telegramBot, never()).execute(any(CopyMessage.class));
        verify(messageSender, never()).sendToTopic(anyLong(), anyInt(), anyString());
    }

    @Test
    void shouldRecreateTopicWhenFirstCopyFails() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse failResponse = failResponse();
        MessageIdResponse okResponse = okResponse();

        when(telegramBot.execute(any(CopyMessage.class)))
                .thenReturn(failResponse)
                .thenReturn(okResponse);

        when(topicManager.recreateStaleTopic(1L, "@johndoe", 42)).thenReturn(99);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).recreateStaleTopic(1L, "@johndoe", 42);
        verify(telegramBot, times(2)).execute(any(CopyMessage.class));
        verify(messageSender).sendToTopic(eq(100L), eq(99), anyString());
    }

    @Test
    void shouldNotSendResponseWhenRecreationAlsoFails() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse failResponse = failResponse();

        when(telegramBot.execute(any(CopyMessage.class)))
                .thenReturn(failResponse)
                .thenReturn(failResponse);

        when(topicManager.recreateStaleTopic(1L, "@johndoe", 42)).thenReturn(99);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(messageSender, never()).sendToTopic(anyLong(), anyInt(), anyString());
    }

    @Test
    void shouldIncludeAdminTagWhenEscalationNeeded() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarderWithAdmin("admin");
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", true);

        verify(messageSender).sendToTopic(eq(100L), eq(42), contains("@admin"));
    }

    @Test
    void shouldNotIncludeAdminTagWhenNoEscalation() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarderWithAdmin("admin");
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(messageSender).sendToTopic(eq(100L), eq(42), argThat(s -> !s.contains("@admin")));
    }

    @Test
    void shouldResolveUserNameFromUsername() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).resolveTopicId(1L, "@johndoe");
    }

    @Test
    void shouldResolveUserNameFromFirstAndLastName() {
        User user = userWithNames(2L, "John", "Doe");
        when(topicManager.resolveTopicId(2L, "John Doe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(2L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).resolveTopicId(2L, "John Doe");
    }

    @Test
    void shouldResolveUserNameFromFirstNameOnly() {
        User user = userWithNames(3L, "John", null);
        when(topicManager.resolveTopicId(3L, "John")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(3L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).resolveTopicId(3L, "John");
    }

    @Test
    void shouldResolveUserNameAsFallback() {
        User user = userWithNames(4L, null, null);
        when(topicManager.resolveTopicId(4L, "User 4")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(4L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).resolveTopicId(4L, "User 4");
    }

    @Test
    void shouldTruncateLongBotResponse() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class))).thenReturn(okResponse);

        SupportGroupForwarder forwarder = createForwarder();
        String longResponse = "A".repeat(4000);
        forwarder.forwardToSupport(1L, 100, user, "Help me!", longResponse, false);

        verify(messageSender).sendToTopic(eq(100L), eq(42), contains("(сообщение обрезано)"));
    }

    @Test
    void shouldForwardErrorToTopic() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardErrorToTopic(user, "User message", "User visible msg", "Error details");

        verify(messageSender, times(2)).sendToTopic(eq(100L), eq(42), anyString());
    }

    @Test
    void shouldNotForwardErrorWhenTopicIsNull() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(null);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardErrorToTopic(user, "User message", "User visible msg", "Error details");

        verify(messageSender, never()).sendToTopic(anyLong(), anyInt(), anyString());
    }

    @Test
    void shouldTruncateLongUserMessageInErrorForwarding() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        SupportGroupForwarder forwarder = createForwarder();
        String longMessage = "B".repeat(500);
        forwarder.forwardErrorToTopic(user, longMessage, "User visible msg", "Error details");

        verify(messageSender).sendToTopic(eq(100L), eq(42), contains("..."));
    }

    @Test
    void shouldCopyMessageExceptionBeHandled() {
        User user = userWithUsername(1L, "johndoe");
        when(topicManager.resolveTopicId(1L, "@johndoe")).thenReturn(42);

        MessageIdResponse okResponse = okResponse();
        when(telegramBot.execute(any(CopyMessage.class)))
                .thenThrow(new RuntimeException("Timeout"))
                .thenReturn(okResponse);

        when(topicManager.recreateStaleTopic(1L, "@johndoe", 42)).thenReturn(99);

        SupportGroupForwarder forwarder = createForwarder();
        forwarder.forwardToSupport(1L, 100, user, "Help me!", "Bot response", false);

        verify(topicManager).recreateStaleTopic(1L, "@johndoe", 42);
    }

    private MessageIdResponse okResponse() {
        MessageIdResponse response = mock(MessageIdResponse.class);
        when(response.isOk()).thenReturn(true);
        return response;
    }

    private MessageIdResponse failResponse() {
        MessageIdResponse response = mock(MessageIdResponse.class);
        when(response.isOk()).thenReturn(false);
        when(response.description()).thenReturn("Topic not found");
        return response;
    }

    private SupportGroupForwarder createForwarder() {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(100L);
        return new SupportGroupForwarder(telegramBot, messageSender, topicManager, properties);
    }

    private SupportGroupForwarder createForwarderWithAdmin(String adminUsername) {
        TelegramProperties properties = new TelegramProperties();
        properties.setSupportGroupChatId(100L);
        properties.setSupportAdminUsername(adminUsername);
        return new SupportGroupForwarder(telegramBot, messageSender, topicManager, properties);
    }

    private User userWithUsername(long id, String username) {
        User user = mock(User.class);
        when(user.id()).thenReturn(id);
        when(user.username()).thenReturn(username);
        return user;
    }

    private User userWithNames(long id, String firstName, String lastName) {
        User user = mock(User.class);
        when(user.id()).thenReturn(id);
        when(user.username()).thenReturn(null);
        when(user.firstName()).thenReturn(firstName);
        when(user.lastName()).thenReturn(lastName);
        return user;
    }
}
