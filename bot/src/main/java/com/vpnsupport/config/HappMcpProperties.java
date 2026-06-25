package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "happ.mcp")
public class HappMcpProperties {

    private String url = "https://www.happ.su/main/~gitbook/mcp";
    private boolean enabled = true;

    public String getUrl() {
        return url;
    }

    public void setUrl(String url) {
        this.url = url;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }
}
