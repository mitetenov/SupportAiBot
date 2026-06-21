package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "telegram")
public class TelegramProperties {

    private String botToken;
    private long supportGroupChatId;
    private String supportAdminUsername;

    public String getBotToken() {
        return botToken;
    }

    public void setBotToken(String botToken) {
        this.botToken = botToken;
    }

    public long getSupportGroupChatId() {
        return supportGroupChatId;
    }

    public void setSupportGroupChatId(long supportGroupChatId) {
        this.supportGroupChatId = supportGroupChatId;
    }

    public String getSupportAdminUsername() {
        return supportAdminUsername;
    }

    public void setSupportAdminUsername(String supportAdminUsername) {
        this.supportAdminUsername = supportAdminUsername;
    }
}
