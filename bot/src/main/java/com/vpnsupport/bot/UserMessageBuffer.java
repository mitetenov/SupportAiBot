package com.vpnsupport.bot;

import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.User;
import com.vpnsupport.config.MessageBufferProperties;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/**
 * Coalesces messages a user sends in quick succession into a single batch.
 *
 * <p>People type a thought across three messages. Handling each one separately
 * meant the bot answered the first fragment and, because of the per-message rate
 * limit, silently dropped the rest. Waiting a beat and merging them produces one
 * coherent answer, costs fewer tokens, and — most importantly — loses nothing.
 *
 * <p>The batch is flushed when the user stops typing for
 * {@code telegram.buffer.window}, or as soon as it reaches
 * {@code telegram.buffer.max-messages}, so a flood can't defer processing
 * indefinitely.
 */
@Component
public class UserMessageBuffer {

    private static final Logger log = LoggerFactory.getLogger(UserMessageBuffer.class);

    private final Map<Long, Batch> pending = new ConcurrentHashMap<>();
    private final ScheduledExecutorService scheduler;
    private final long windowMillis;
    private final int maxMessages;

    public UserMessageBuffer(MessageBufferProperties properties) {
        this.windowMillis = properties.getWindow().toMillis();
        this.maxMessages = properties.getMaxMessages();
        this.scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "user-message-buffer");
            thread.setDaemon(true);
            return thread;
        });
    }

    /**
     * Adds {@code message} to the user's pending batch and (re)arms the flush
     * timer. {@code sink} receives the merged batch on the scheduler thread and
     * is expected to hand off real work to an executor.
     */
    public void submit(long userId, BufferedMessage message, Consumer<MessageBatch> sink) {
        boolean flushNow;

        synchronized (pending) {
            Batch batch = pending.computeIfAbsent(userId, id -> new Batch());
            batch.messages.add(message);
            batch.generation++;
            flushNow = batch.messages.size() >= maxMessages;

            if (!flushNow) {
                long generation = batch.generation;
                scheduler.schedule(() -> flush(userId, generation, sink),
                        windowMillis, TimeUnit.MILLISECONDS);
            }
        }

        if (flushNow) {
            flush(userId, -1, sink);
        }
    }

    /**
     * @param generation the batch revision this flush was scheduled for, or -1
     *                   to flush unconditionally; a stale generation means more
     *                   messages arrived and a later flush is already armed
     */
    private void flush(long userId, long generation, Consumer<MessageBatch> sink) {
        MessageBatch batch;

        synchronized (pending) {
            Batch current = pending.get(userId);
            if (current == null || current.messages.isEmpty()) {
                return;
            }
            if (generation >= 0 && current.generation != generation) {
                return;
            }
            pending.remove(userId);
            batch = MessageBatch.of(current.messages);
        }

        try {
            sink.accept(batch);
        } catch (Exception e) {
            log.error("Failed to dispatch buffered messages for user {}", userId, e);
        }
    }

    @PreDestroy
    public void shutdown() {
        scheduler.shutdownNow();
    }

    private static final class Batch {
        private final List<BufferedMessage> messages = new ArrayList<>();
        private long generation;
    }

    /** One Telegram message waiting to be merged. */
    public record BufferedMessage(Message message, String text, String base64Image, String mimeType) {

        public static BufferedMessage text(Message message, String text) {
            return new BufferedMessage(message, text, null, null);
        }
    }

    /**
     * Consecutive messages merged into one request.
     *
     * @param text          the merged text, in the order the user sent it
     * @param messageIds    every original Telegram message ID, so all of them
     *                      reach the support topic
     * @param base64Image   the image from the batch, if any
     */
    public record MessageBatch(Message lastMessage, User user, String text, List<Integer> messageIds,
                               String base64Image, String mimeType) {

        static MessageBatch of(List<BufferedMessage> messages) {
            BufferedMessage last = messages.get(messages.size() - 1);
            List<Integer> ids = messages.stream().map(m -> m.message().messageId()).toList();

            String merged = messages.stream()
                    .map(BufferedMessage::text)
                    .filter(t -> t != null && !t.isBlank())
                    .reduce((a, b) -> a + "\n" + b)
                    .orElse("");

            // At most one image per batch: the model gets a single screenshot
            // plus whatever text accompanied it.
            BufferedMessage withImage = messages.stream()
                    .filter(m -> m.base64Image() != null)
                    .findFirst()
                    .orElse(null);

            return new MessageBatch(
                    last.message(),
                    last.message().from(),
                    merged,
                    ids,
                    withImage != null ? withImage.base64Image() : null,
                    withImage != null ? withImage.mimeType() : null);
        }

        public boolean hasImage() {
            return base64Image != null;
        }

        public int size() {
            return messageIds.size();
        }
    }
}
