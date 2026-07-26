package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.request.SendChatAction;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Keeps the "typing…" status alive for the duration of a request.
 *
 * <p>Telegram clears the status after about five seconds. A single call at the
 * start left the user staring at silence while a chain of tool calls ran, with
 * no way to tell a slow answer from a dead bot.
 */
@Component
public class TypingIndicator {

    private static final Logger log = LoggerFactory.getLogger(TypingIndicator.class);
    private static final long REFRESH_SECONDS = 4;

    private final TelegramBot telegramBot;
    private final ScheduledExecutorService scheduler;

    public TypingIndicator(TelegramBot telegramBot) {
        this.telegramBot = telegramBot;
        this.scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "typing-indicator");
            thread.setDaemon(true);
            return thread;
        });
    }

    /** Starts showing "typing…" in {@code chatId} until the handle is closed. */
    public Session start(long chatId) {
        send(chatId);
        ScheduledFuture<?> task = scheduler.scheduleAtFixedRate(
                () -> send(chatId), REFRESH_SECONDS, REFRESH_SECONDS, TimeUnit.SECONDS);
        return () -> task.cancel(false);
    }

    private void send(long chatId) {
        try {
            telegramBot.execute(new SendChatAction(chatId, "typing"));
        } catch (Exception e) {
            log.debug("Failed to send typing action to {}: {}", chatId, e.getMessage());
        }
    }

    @PreDestroy
    public void shutdown() {
        scheduler.shutdownNow();
    }

    /** Stops the indicator. Safe to use in try-with-resources. */
    @FunctionalInterface
    public interface Session extends AutoCloseable {
        @Override
        void close();
    }
}
