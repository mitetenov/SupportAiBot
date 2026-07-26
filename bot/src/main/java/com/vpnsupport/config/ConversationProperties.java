package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "conversation")
public class ConversationProperties {

    /**
     * How long the AI stays quiet after an operator replies, so a human handover
     * is not interrupted by the bot.
     */
    private Duration operatorSuppressionWindow = Duration.ofMinutes(30);

    /** How long a user's last question is kept for /operator attribution. */
    private Duration lastQueryTtl = Duration.ofHours(6);

    public Duration getOperatorSuppressionWindow() {
        return operatorSuppressionWindow;
    }

    public void setOperatorSuppressionWindow(Duration operatorSuppressionWindow) {
        this.operatorSuppressionWindow = operatorSuppressionWindow;
    }

    public Duration getLastQueryTtl() {
        return lastQueryTtl;
    }

    public void setLastQueryTtl(Duration lastQueryTtl) {
        this.lastQueryTtl = lastQueryTtl;
    }
}
