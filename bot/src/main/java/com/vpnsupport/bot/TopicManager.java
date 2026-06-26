package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.request.CreateForumTopic;
import com.pengrad.telegrambot.response.CreateForumTopicResponse;
import com.vpnsupport.config.TelegramProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;

@Component
public class TopicManager {

    private static final Logger log = LoggerFactory.getLogger(TopicManager.class);

    private final TopicMappingRepository repository;
    private final TelegramBot telegramBot;
    private final long supportGroupChatId;
    private final ConcurrentHashMap<Long, Object> userLocks = new ConcurrentHashMap<>();

    public TopicManager(TopicMappingRepository repository, TelegramBot telegramBot,
                        TelegramProperties properties) {
        this.repository = repository;
        this.telegramBot = telegramBot;
        this.supportGroupChatId = properties.getSupportGroupChatId();
    }

    public Integer resolveTopicId(Long userId, String userName) {
        Object lock = userLocks.computeIfAbsent(userId, id -> new Object());
        synchronized (lock) {
            return repository.findById(userId)
                    .map(TopicMapping::getTopicId)
                    .orElseGet(() -> createTopic(userId, userName));
        }
    }

    public Integer recreateStaleTopic(Long userId, String userName, Integer staleTopicId) {
        Object lock = userLocks.computeIfAbsent(userId, id -> new Object());
        synchronized (lock) {
            TopicMapping existing = repository.findById(userId).orElse(null);
            if (existing != null && Objects.equals(existing.getTopicId(), staleTopicId)) {
                repository.deleteById(userId);
                log.info("Deleted stale topic mapping {} for user {}", staleTopicId, userId);
            }
            return createTopic(userId, userName);
        }
    }

    private Integer createTopic(Long userId, String userName) {
        String topicName = buildTopicName(userId, userName);
        log.info("Creating forum topic for user {}: {}", userId, topicName);

        try {
            CreateForumTopicResponse response = telegramBot.execute(
                    new CreateForumTopic(supportGroupChatId, topicName)
            );

            if (response.isOk()) {
                Integer topicId = response.forumTopic().messageThreadId();
                repository.save(new TopicMapping(userId, topicId, userName));
                log.info("Created topic {} for user {}", topicId, userId);
                return topicId;
            } else {
                log.error("Failed to create topic: {} - {}", response.errorCode(), response.description());
                return null;
            }
        } catch (Exception e) {
            log.error("Error creating topic for user {}", userId, e);
            return null;
        }
    }

    private String buildTopicName(Long userId, String userName) {
        if (userName != null && !userName.isBlank()) {
            return userName + " (ID: " + userId + ")";
        }
        return "User " + userId;
    }
}
