package com.vpnsupport.bot;

import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;

@Service
public class ChatHistoryService {

    private static final int MAX_MESSAGES = 20;

    private final ConcurrentHashMap<Long, Deque<Map<String, Object>>> histories = new ConcurrentHashMap<>();

    public List<Map<String, Object>> getHistory(long userId) {
        Deque<Map<String, Object>> history = histories.get(userId);
        if (history == null || history.isEmpty()) {
            return List.of();
        }
        return List.copyOf(history);
    }

    public void addUserMessage(long userId, String text) {
        append(userId, Map.of("role", "user", "content", text));
    }

    public void addAssistantMessage(long userId, String text) {
        append(userId, Map.of("role", "assistant", "content", text));
    }

    private void append(long userId, Map<String, Object> message) {
        Deque<Map<String, Object>> history = histories.computeIfAbsent(userId, id -> new ConcurrentLinkedDeque<>());
        synchronized (history) {
            history.addLast(message);
            while (history.size() > MAX_MESSAGES) {
                history.removeFirst();
            }
        }
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
    }
}
