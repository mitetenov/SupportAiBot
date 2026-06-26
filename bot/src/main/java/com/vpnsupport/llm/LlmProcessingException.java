package com.vpnsupport.llm;

public class LlmProcessingException extends RuntimeException {

    private final String userFriendlyMessage;

    public LlmProcessingException(String message, String userFriendlyMessage) {
        super(message);
        this.userFriendlyMessage = userFriendlyMessage;
    }

    public LlmProcessingException(String message, String userFriendlyMessage, Throwable cause) {
        super(message, cause);
        this.userFriendlyMessage = userFriendlyMessage;
    }

    public String getUserFriendlyMessage() {
        return userFriendlyMessage;
    }
}
