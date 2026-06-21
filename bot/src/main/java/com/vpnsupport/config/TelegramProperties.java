package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;

@ConfigurationProperties(prefix = "telegram")
public class TelegramProperties {

    private String botToken;
    private long supportGroupChatId;
    private String supportAdminUsername;
    private Set<Long> supportAdminTelegramIds = Set.of();

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

    public Set<Long> getSupportAdminTelegramIds() {
        return supportAdminTelegramIds;
    }

    public void setSupportAdminTelegramIds(String supportAdminTelegramIds) {
        if (supportAdminTelegramIds == null || supportAdminTelegramIds.isBlank()) {
            this.supportAdminTelegramIds = Set.of();
        } else {
            this.supportAdminTelegramIds = Stream.of(supportAdminTelegramIds.split(","))
                    .map(String::trim)
                    .filter(s -> !s.isEmpty())
                    .map(Long::parseLong)
                    .collect(Collectors.toUnmodifiableSet());
        }
    }
}
