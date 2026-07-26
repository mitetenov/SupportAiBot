package com.vpnsupport.llm;

public interface LlmClient {

    LlmReply chat(String userMessage, long telegramUserId);

    LlmReply chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType);

    default boolean supportsImages() {
        return false;
    }
}
