package com.vpnsupport.bot;

import com.pengrad.telegrambot.model.Chat;
import com.pengrad.telegrambot.model.Message;
import com.pengrad.telegrambot.model.User;
import com.vpnsupport.bot.UserMessageBuffer.BufferedMessage;
import com.vpnsupport.bot.UserMessageBuffer.MessageBatch;
import com.vpnsupport.config.MessageBufferProperties;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.mock;

class UserMessageBufferTest {

    private static final long USER_ID = 100L;

    private UserMessageBuffer buffer;
    private final CopyOnWriteArrayList<MessageBatch> delivered = new CopyOnWriteArrayList<>();

    @BeforeEach
    void setUp() {
        MessageBufferProperties properties = new MessageBufferProperties();
        properties.setWindow(Duration.ofMillis(120));
        properties.setMaxMessages(3);
        buffer = new UserMessageBuffer(properties);
    }

    @AfterEach
    void tearDown() {
        buffer.shutdown();
    }

    private Message message(int messageId) {
        Chat chat = mock(Chat.class);
        lenient().when(chat.id()).thenReturn(USER_ID);
        User user = mock(User.class);
        lenient().when(user.id()).thenReturn(USER_ID);

        Message msg = mock(Message.class);
        lenient().when(msg.chat()).thenReturn(chat);
        lenient().when(msg.from()).thenReturn(user);
        lenient().when(msg.messageId()).thenReturn(messageId);
        return msg;
    }

    private void submit(int messageId, String text) {
        buffer.submit(USER_ID, BufferedMessage.text(message(messageId), text), delivered::add);
    }

    private void awaitDelivery() throws InterruptedException {
        for (int i = 0; i < 100 && delivered.isEmpty(); i++) {
            TimeUnit.MILLISECONDS.sleep(20);
        }
    }

    @Test
    void shouldMergeMessagesSentInQuickSuccession() throws Exception {
        submit(1, "привет");
        submit(2, "не работает впн");
        submit(3, "что делать");

        awaitDelivery();

        assertEquals(1, delivered.size(), "a typing burst must produce a single request");
        MessageBatch batch = delivered.get(0);
        assertEquals("привет\nне работает впн\nчто делать", batch.text());
        assertEquals(List.of(1, 2, 3), batch.messageIds());
    }

    @Test
    void shouldFlushImmediatelyWhenTheBatchIsFull() throws Exception {
        submit(1, "one");
        submit(2, "two");
        submit(3, "three");

        // maxMessages is 3, so this lands well before the 120ms window elapses.
        for (int i = 0; i < 20 && delivered.isEmpty(); i++) {
            TimeUnit.MILLISECONDS.sleep(2);
        }

        assertEquals(1, delivered.size());
        assertEquals(3, delivered.get(0).size());
    }

    @Test
    void shouldDeliverASingleMessageAfterTheWindow() throws Exception {
        submit(1, "один вопрос");

        awaitDelivery();

        assertEquals(1, delivered.size());
        assertEquals("один вопрос", delivered.get(0).text());
        assertEquals(List.of(1), delivered.get(0).messageIds());
    }

    @Test
    void shouldStartAFreshBatchAfterAFlush() throws Exception {
        submit(1, "первый");
        awaitDelivery();
        delivered.clear();

        submit(2, "второй");
        awaitDelivery();

        assertEquals(1, delivered.size());
        assertEquals("второй", delivered.get(0).text());
    }

    @Test
    void shouldCarryTheImageFromABatch() throws Exception {
        buffer.submit(USER_ID, BufferedMessage.text(message(1), "смотри"), delivered::add);
        buffer.submit(USER_ID, new BufferedMessage(message(2), "скриншот", "BASE64", "image/png"),
                delivered::add);

        awaitDelivery();

        MessageBatch batch = delivered.get(0);
        assertTrue(batch.hasImage());
        assertEquals("BASE64", batch.base64Image());
        assertEquals("image/png", batch.mimeType());
        assertEquals("смотри\nскриншот", batch.text());
    }

    @Test
    void shouldKeepDifferentUsersInSeparateBatches() throws Exception {
        buffer.submit(USER_ID, BufferedMessage.text(message(1), "от первого"), delivered::add);
        buffer.submit(200L, BufferedMessage.text(message(2), "от второго"), delivered::add);

        for (int i = 0; i < 100 && delivered.size() < 2; i++) {
            TimeUnit.MILLISECONDS.sleep(20);
        }

        assertEquals(2, delivered.size());
    }

    /**
     * Messages arriving from several threads while flushes are firing must not
     * be lost or duplicated — that is what the generation counter guards, and
     * the sequential tests above never exercise it.
     */
    @Test
    void shouldNeitherLoseNorDuplicateMessagesUnderConcurrentSubmits() throws Exception {
        MessageBufferProperties properties = new MessageBufferProperties();
        properties.setWindow(Duration.ofMillis(5));
        properties.setMaxMessages(4);
        UserMessageBuffer concurrent = new UserMessageBuffer(properties);

        int threads = 8;
        int perThread = 40;
        int expected = threads * perThread;

        CopyOnWriteArrayList<MessageBatch> batches = new CopyOnWriteArrayList<>();
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);

        try {
            for (int t = 0; t < threads; t++) {
                int base = t * perThread;
                pool.execute(() -> {
                    try {
                        start.await();
                        for (int i = 0; i < perThread; i++) {
                            concurrent.submit(USER_ID,
                                    BufferedMessage.text(message(base + i), "m" + (base + i)),
                                    batches::add);
                            Thread.sleep(1);
                        }
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    } finally {
                        done.countDown();
                    }
                });
            }

            start.countDown();
            assertTrue(done.await(30, TimeUnit.SECONDS), "producers did not finish");

            // Let the final window elapse and the last flush land.
            for (int i = 0; i < 100 && countMessages(batches) < expected; i++) {
                TimeUnit.MILLISECONDS.sleep(20);
            }

            Set<Integer> seen = new HashSet<>();
            int total = 0;
            for (MessageBatch batch : batches) {
                for (Integer id : batch.messageIds()) {
                    assertTrue(seen.add(id), "message " + id + " was delivered twice");
                    total++;
                }
            }
            assertEquals(expected, total, "every submitted message must be delivered exactly once");
        } finally {
            pool.shutdownNow();
            concurrent.shutdown();
        }
    }

    private static int countMessages(List<MessageBatch> batches) {
        return batches.stream().mapToInt(MessageBatch::size).sum();
    }
}
