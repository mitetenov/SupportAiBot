package com.vpnsupport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "remnawave.mcp")
public class RemnawaveMcpProperties {

    private String url;
    private String baseUrl;
    private String apiToken;
    /**
     * When true, mutating MCP tools (HWID device deletion) are withheld from
     * the model entirely. Defaults to false to preserve the device-management
     * flow the support prompt describes.
     */
    private boolean readonly = false;

    public String getUrl() {
        return url;
    }

    public void setUrl(String url) {
        this.url = url;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getApiToken() {
        return apiToken;
    }

    public void setApiToken(String apiToken) {
        this.apiToken = apiToken;
    }

    public boolean isReadonly() {
        return readonly;
    }

    public void setReadonly(boolean readonly) {
        this.readonly = readonly;
    }
}
