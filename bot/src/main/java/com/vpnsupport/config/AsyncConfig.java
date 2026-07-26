package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/**
 * Separate pools for the two very different kinds of async work in the bot.
 *
 * <p>They used to share Spring's single auto-configured executor, so a handful
 * of concurrent model calls — each up to a 60-second timeout — would sit in
 * front of the queue and stall chat-history persistence behind them.
 */
@Configuration
public class AsyncConfig {

    /**
     * Handles Telegram updates, including the blocking LLM and MCP calls.
     * Bounded queue with a caller-runs policy: under a flood, backpressure
     * reaches the Telegram polling loop instead of the queue growing without
     * limit.
     */
    @Bean
    @Primary
    @ConfigurationProperties(prefix = "task.updates")
    public TaskExecutor taskExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(8);
        executor.setMaxPoolSize(32);
        executor.setQueueCapacity(200);
        executor.setThreadNamePrefix("bot-update-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        executor.initialize();
        return executor;
    }

    /** Short database writes: chat-history persistence and cleanup. */
    @Bean("persistenceExecutor")
    @ConfigurationProperties(prefix = "task.persistence")
    public TaskExecutor persistenceExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(2);
        executor.setMaxPoolSize(4);
        executor.setQueueCapacity(1000);
        executor.setThreadNamePrefix("bot-persist-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        executor.initialize();
        return executor;
    }
}
