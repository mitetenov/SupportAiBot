package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.ForumTopic;
import com.pengrad.telegrambot.request.CreateForumTopic;
import com.pengrad.telegrambot.response.CreateForumTopicResponse;
import com.vpnsupport.config.TelegramProperties;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Optional;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TopicManagerTest {

    @Mock
    private TopicMappingRepository repository;

    @Mock
    private TelegramBot telegramBot;

    private TelegramProperties properties;
    private TopicManager topicManager;

    @BeforeEach
    void setUp() {
        properties = new TelegramProperties();
        properties.setSupportGroupChatId(100L);
        topicManager = new TopicManager(repository, telegramBot, properties);
    }

    @Test
    void shouldReturnExistingTopicId() {
        when(repository.findById(1L)).thenReturn(Optional.of(new TopicMapping(1L, 42, "user1")));

        Integer topicId = topicManager.resolveTopicId(1L, "user1");

        assertEquals(42, topicId);
        verify(repository).findById(1L);
        verify(telegramBot, never()).execute(any());
    }

    @Test
    void shouldCreateTopicWhenNotFound() {
        when(repository.findById(1L)).thenReturn(Optional.empty());

        ForumTopic forumTopic = mock(ForumTopic.class);
        when(forumTopic.messageThreadId()).thenReturn(55);
        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(true);
        when(response.forumTopic()).thenReturn(forumTopic);
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        Integer topicId = topicManager.resolveTopicId(1L, "testuser");

        assertEquals(55, topicId);
        verify(repository).save(any(TopicMapping.class));
    }

    @Test
    void shouldReturnNullWhenTopicCreationFails() {
        when(repository.findById(1L)).thenReturn(Optional.empty());

        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(false);
        when(response.errorCode()).thenReturn(400);
        when(response.description()).thenReturn("Bad request");
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        Integer topicId = topicManager.resolveTopicId(1L, "testuser");

        assertNull(topicId);
        verify(repository, never()).save(any());
    }

    @Test
    void shouldReturnNullWhenTopicCreationThrows() {
        when(repository.findById(1L)).thenReturn(Optional.empty());
        when(telegramBot.execute(any(CreateForumTopic.class))).thenThrow(new RuntimeException("Network error"));

        Integer topicId = topicManager.resolveTopicId(1L, "testuser");

        assertNull(topicId);
        verify(repository, never()).save(any());
    }

    @Test
    void shouldBuildTopicNameWithUserName() {
        String topicName = callBuildTopicName(1L, "johndoe");
        assertEquals("johndoe (ID: 1)", topicName);
    }

    @Test
    void shouldBuildTopicNameWithoutUserName() {
        String topicName = callBuildTopicName(2L, null);
        assertEquals("User 2", topicName);
    }

    @Test
    void shouldBuildTopicNameWithBlankUserName() {
        String topicName = callBuildTopicName(3L, "   ");
        assertEquals("User 3", topicName);
    }

    @Test
    void shouldBuildTopicNameWithEmptyUserName() {
        String topicName = callBuildTopicName(4L, "");
        assertEquals("User 4", topicName);
    }

    @Test
    void shouldRecreateStaleTopic() {
        when(repository.findById(1L)).thenReturn(Optional.of(new TopicMapping(1L, 42, "user1")));

        ForumTopic forumTopic = mock(ForumTopic.class);
        when(forumTopic.messageThreadId()).thenReturn(99);
        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(true);
        when(response.forumTopic()).thenReturn(forumTopic);
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        Integer newTopicId = topicManager.recreateStaleTopic(1L, "user1", 42);

        assertEquals(99, newTopicId);
        verify(repository).deleteById(1L);
        verify(repository).save(any(TopicMapping.class));
    }

    @Test
    void shouldNotDeleteMappingIfTopicIdChanged() {
        when(repository.findById(1L)).thenReturn(Optional.of(new TopicMapping(1L, 100, "user1")));

        ForumTopic forumTopic = mock(ForumTopic.class);
        when(forumTopic.messageThreadId()).thenReturn(200);
        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(true);
        when(response.forumTopic()).thenReturn(forumTopic);
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        topicManager.recreateStaleTopic(1L, "user1", 42);

        verify(repository, never()).deleteById(any());
    }

    @Test
    void shouldRecreateTopicEvenWhenNoExistingMapping() {
        when(repository.findById(1L)).thenReturn(Optional.empty());

        ForumTopic forumTopic = mock(ForumTopic.class);
        when(forumTopic.messageThreadId()).thenReturn(77);
        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(true);
        when(response.forumTopic()).thenReturn(forumTopic);
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        Integer topicId = topicManager.recreateStaleTopic(1L, "user1", 42);

        assertEquals(77, topicId);
        verify(repository, never()).deleteById(any());
    }

    @Test
    void shouldBeThreadSafe() throws Exception {
        when(repository.findById(1L)).thenReturn(Optional.of(new TopicMapping(1L, 10, "user1")));

        int threadCount = 10;
        ExecutorService executor = Executors.newFixedThreadPool(threadCount);
        CountDownLatch latch = new CountDownLatch(threadCount);

        for (int i = 0; i < threadCount; i++) {
            executor.submit(() -> {
                try {
                    topicManager.resolveTopicId(1L, "user1");
                } finally {
                    latch.countDown();
                }
            });
        }

        assertTrue(latch.await(5, TimeUnit.SECONDS));
        executor.shutdown();

        verify(repository, atLeastOnce()).findById(1L);
    }

    @Test
    void shouldUseCorrectChatIdForTopicCreation() {
        when(repository.findById(1L)).thenReturn(Optional.empty());

        ForumTopic forumTopic = mock(ForumTopic.class);
        when(forumTopic.messageThreadId()).thenReturn(33);
        CreateForumTopicResponse response = mock(CreateForumTopicResponse.class);
        when(response.isOk()).thenReturn(true);
        when(response.forumTopic()).thenReturn(forumTopic);
        when(telegramBot.execute(any(CreateForumTopic.class))).thenReturn(response);

        topicManager.resolveTopicId(1L, "user1");

        ArgumentCaptor<CreateForumTopic> captor = ArgumentCaptor.forClass(CreateForumTopic.class);
        verify(telegramBot).execute(captor.capture());
        CreateForumTopic request = captor.getValue();
        assertEquals(100L, Long.parseLong(String.valueOf(request.getParameters().get("chat_id"))));
    }

    private String callBuildTopicName(Long userId, String userName) {
        try {
            var method = TopicManager.class.getDeclaredMethod("buildTopicName", Long.class, String.class);
            method.setAccessible(true);
            return (String) method.invoke(topicManager, userId, userName);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
