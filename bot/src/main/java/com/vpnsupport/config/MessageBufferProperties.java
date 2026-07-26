package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "telegram.buffer")
public class MessageBufferProperties {

    /** How long to wait for the user to keep typing before answering. */
    private Duration window = Duration.ofMillis(2500);

    /** Flush immediately once this many messages are pending. */
    private int maxMessages = 5;

    public Duration getWindow() {
        return window;
    }

    public void setWindow(Duration window) {
        this.window = window;
    }

    public int getMaxMessages() {
        return maxMessages;
    }

    public void setMaxMessages(int maxMessages) {
        this.maxMessages = maxMessages;
    }
}
