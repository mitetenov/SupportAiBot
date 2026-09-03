package com.vpnsupport.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class StartupValidatorTest {

    @Test
    void shouldValidateDeepseekProvider() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldValidateGeminiProvider() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("gemini");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        gemini.setApiKey("gemini-key");
        gemini.setModel("gemini-pro");

        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldValidateOpenAiProvider() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("openai");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        openAi.setApiKey("sk-openai-key");
        openAi.setModel("gpt-4");

        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldValidateGroqProvider() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("groq");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        GroqProperties groq = new GroqProperties();
        groq.setApiKey("groq-key");
        groq.setModel("llama-3.3-70b-versatile");
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(
                telegram, llm, deepSeek, gemini, remnawave, openAi, groq);

        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldThrowWhenGroqApiKeyMissing() {
        StartupValidator validator = groqValidator(null, "llama-3.3-70b-versatile");

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));

        assertEquals("GROQ_API_KEY is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenGroqModelMissing() {
        StartupValidator validator = groqValidator("groq-key", " ");

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));

        assertEquals("GROQ_MODEL is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenBotTokenMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("TELEGRAM_BOT_TOKEN is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenSupportGroupChatIdIsZero() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("TELEGRAM_SUPPORT_GROUP_CHAT_ID is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenDeepseekApiKeyMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("DEEPSEEK_API_KEY is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenDeepseekModelMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("DEEPSEEK_MODEL is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenOpenAiApiKeyMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("openai");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        openAi.setModel("gpt-4");

        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("OPENAI_API_KEY is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenOpenAiModelMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("openai");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        openAi.setApiKey("sk-openai-key");

        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("OPENAI_MODEL is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenRemnawaveBaseUrlMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("REMNAWAVE_BASE_URL is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenRemnawaveApiTokenMissing() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("deepseek");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        deepSeek.setApiKey("sk-key");
        deepSeek.setModel("deepseek-chat");

        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("REMNAWAVE_API_TOKEN is required", ex.getMessage());
    }

    @Test
    void shouldThrowForUnknownProvider() {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("unknown");

        DeepSeekProperties deepSeek = new DeepSeekProperties();
        GeminiProperties gemini = new GeminiProperties();
        OpenAiProperties openAi = new OpenAiProperties();
        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("Unknown LLM provider: unknown", ex.getMessage());
    }

    private StartupValidator groqValidator(String apiKey, String model) {
        TelegramProperties telegram = new TelegramProperties();
        telegram.setBotToken("token123");
        telegram.setSupportGroupChatId(123L);

        LlmProperties llm = new LlmProperties();
        llm.setProvider("groq");

        GroqProperties groq = new GroqProperties();
        groq.setApiKey(apiKey);
        groq.setModel(model);

        RemnawaveMcpProperties remnawave = new RemnawaveMcpProperties();
        remnawave.setBaseUrl("https://example.com");
        remnawave.setApiToken("api-token");

        return new StartupValidator(telegram, llm, new DeepSeekProperties(), new GeminiProperties(), remnawave,
                new OpenAiProperties(), groq);
    }
}
