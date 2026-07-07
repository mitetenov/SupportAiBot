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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        assertDoesNotThrow(() -> validator.run(null));
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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

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
        remnawave.setUrl("http://mcp:3100");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("OPENAI_MODEL is required", ex.getMessage());
    }

    @Test
    void shouldThrowWhenRemnawaveMcpUrlMissing() {
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

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("REMNAWAVE_MCP_URL is required", ex.getMessage());
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
        remnawave.setUrl("http://mcp:3100");

        StartupValidator validator = new StartupValidator(telegram, llm, deepSeek, gemini, remnawave, openAi);

        IllegalStateException ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertEquals("Unknown LLM provider: unknown", ex.getMessage());
    }
}
