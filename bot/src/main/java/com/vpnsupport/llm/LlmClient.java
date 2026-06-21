package com.vpnsupport.llm;

public interface LlmClient {

    String chat(String userMessage, long telegramUserId);

    String chatWithImage(String userMessage, long telegramUserId, String base64Image, String mimeType);

    default boolean supportsImages() {
        return false;
    }
}
