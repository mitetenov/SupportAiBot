package com.vpnsupport.bot;

import com.vpnsupport.config.ChatHistoryProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;

@Service
public class ChatHistoryService {

    private static final Logger log = LoggerFactory.getLogger(ChatHistoryService.class);

    private final int maxMessages;
    private final int ttlDays;

    private final ConcurrentHashMap<Long, Deque<Map<String, Object>>> histories = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<Long, Long> lastActivity = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<Long, Boolean> loadedFromDb = new ConcurrentHashMap<>();

    private final ChatMessageRepository chatMessageRepository;
    private final TaskExecutor taskExecutor;

    public ChatHistoryService(ChatMessageRepository chatMessageRepository,
                              TaskExecutor taskExecutor,
                              ChatHistoryProperties properties) {
        this.chatMessageRepository = chatMessageRepository;
        this.taskExecutor = taskExecutor;
        this.maxMessages = properties.getMaxMessages();
        this.ttlDays = properties.getTtlDays();
    }

    public List<Map<String, Object>> getHistory(long userId) {
        Deque<Map<String, Object>> history = histories.get(userId);
        if (history == null) {
            history = loadFromDatabase(userId);
        }
        if (history == null || history.isEmpty()) {
            return List.of();
        }
        lastActivity.put(userId, System.currentTimeMillis());
        return List.copyOf(history);
    }

    public void addUserMessage(long userId, String text) {
        append(userId, Map.of("role", "user", "content", text));
    }

    public void addAssistantMessage(long userId, String text) {
        append(userId, Map.of("role", "assistant", "content", text));
    }

    private void append(long userId, Map<String, Object> message) {
        lastActivity.put(userId, System.currentTimeMillis());
        Deque<Map<String, Object>> history = histories.computeIfAbsent(userId,
                id -> new ConcurrentLinkedDeque<>());
        loadedFromDb.putIfAbsent(userId, true);
        synchronized (history) {
            history.addLast(message);
            while (history.size() > maxMessages) {
                history.removeFirst();
            }
        }
        persistAsync(userId, (String) message.get("role"), (String) message.get("content"));
    }

    public List<Map<String, Object>> toGeminiContents(long userId) {
        List<Map<String, Object>> history = getHistory(userId);
        if (history.isEmpty()) {
            return new ArrayList<>();
        }

        List<Map<String, Object>> contents = new ArrayList<>();
        for (Map<String, Object> message : history) {
            String role = (String) message.get("role");
            String content = (String) message.get("content");
            String geminiRole = "assistant".equals(role) ? "model" : "user";
            contents.add(Map.of(
                    "role", geminiRole,
                    "parts", List.of(Map.of("text", content))
            ));
        }
        return contents;
    }

    public void clear(long userId) {
        histories.remove(userId);
        lastActivity.remove(userId);
        loadedFromDb.remove(userId);
        taskExecutor.execute(() -> {
            try {
                chatMessageRepository.deleteByTelegramId(userId);
                log.debug("Chat history deleted from DB for user {}", userId);
            } catch (Exception e) {
                log.warn("Failed to delete chat history from DB for user {}: {}", userId, e.getMessage());
            }
        });
        log.debug("Chat history cleared for user {}", userId);
    }

    private Deque<Map<String, Object>> loadFromDatabase(long userId) {
        try {
            List<ChatMessage> messages = chatMessageRepository.findTop20ByTelegramIdOrderByCreatedAtAsc(userId);
            if (messages.isEmpty()) {
                loadedFromDb.put(userId, true);
                return null;
            }

            Deque<Map<String, Object>> history = new ConcurrentLinkedDeque<>();
            for (ChatMessage msg : messages) {
                history.addLast(Map.of("role", msg.getRole(), "content", msg.getContent()));
            }
            histories.put(userId, history);
            loadedFromDb.put(userId, true);
            lastActivity.put(userId, System.currentTimeMillis());
            log.debug("Loaded {} chat messages from DB for user {}", history.size(), userId);
            return history;
        } catch (Exception e) {
            log.warn("Failed to load chat history from DB for user {}: {}", userId, e.getMessage());
            loadedFromDb.put(userId, true);
            return null;
        }
    }

    private void persistAsync(long userId, String role, String content) {
        taskExecutor.execute(() -> {
            try {
                chatMessageRepository.save(new ChatMessage(userId, role, content));
            } catch (Exception e) {
                log.warn("Failed to persist chat message for user {}: {}", userId, e.getMessage());
            }
        });
    }

    @Scheduled(cron = "0 0 * * * *")
    public void evictStaleEntries() {
        Instant cutoff = Instant.now().minus(ttlDays, ChronoUnit.DAYS);
        try {
            chatMessageRepository.deleteByCreatedAtBefore(cutoff);
        } catch (Exception e) {
            log.warn("Failed to evict stale chat messages: {}", e.getMessage());
        }
        long memoryCutoff = System.currentTimeMillis() - (long) ttlDays * 24 * 60 * 60 * 1000L;
        lastActivity.forEach((userId, lastSeen) -> {
            if (lastSeen < memoryCutoff) {
                histories.remove(userId);
                lastActivity.remove(userId);
                loadedFromDb.remove(userId);
                log.debug("Evicted stale in-memory history for user {}", userId);
            }
        });
    }
}
