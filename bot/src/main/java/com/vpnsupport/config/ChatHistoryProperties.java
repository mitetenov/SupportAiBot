package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

@Component
@ConfigurationProperties(prefix = "chat.history")
public class ChatHistoryProperties {

    private int maxMessages = 20;
    private int ttlDays = 7;

    public int getMaxMessages() {
        return maxMessages;
    }

    public void setMaxMessages(int maxMessages) {
        this.maxMessages = maxMessages;
    }

    public int getTtlDays() {
        return ttlDays;
    }

    public void setTtlDays(int ttlDays) {
        this.ttlDays = ttlDays;
    }
}
