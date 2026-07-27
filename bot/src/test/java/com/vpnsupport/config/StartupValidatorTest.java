package com.vpnsupport.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class StartupValidatorTest {

    private static TelegramProperties telegram() {
        TelegramProperties t = new TelegramProperties();
        t.setBotToken("token123");
        t.setSupportGroupChatId(-100123L);
        return t;
    }

    private static LlmProperties llm(String provider) {
        LlmProperties l = new LlmProperties();
        l.setProvider(provider);
        return l;
    }

    private static DeepSeekProperties deepSeek() {
        DeepSeekProperties d = new DeepSeekProperties();
        d.setApiKey("sk-key");
        d.setModel("deepseek-chat");
        return d;
    }

    private static GeminiProperties gemini() {
        GeminiProperties g = new GeminiProperties();
        g.setApiKey("gemini-key");
        g.setModel("gemini-pro");
        return g;
    }

    private static OpenAiProperties openAi() {
        OpenAiProperties o = new OpenAiProperties();
        o.setApiKey("sk-openai-key");
        o.setModel("gpt-4");
        return o;
    }

    private static RemnawaveMcpProperties remnawave() {
        RemnawaveMcpProperties r = new RemnawaveMcpProperties();
        r.setUrl("http://mcp:3100");
        return r;
    }

    private static StartupValidator validator(
            TelegramProperties t, LlmProperties l, DeepSeekProperties d,
            GeminiProperties g, RemnawaveMcpProperties r, OpenAiProperties o, String emb) {
        return new StartupValidator(t, l, d, g, r, o, emb);
    }

    @Test
    void shouldValidateDeepseekProvider() {
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldValidateGeminiProvider() {
        StartupValidator validator = validator(telegram(), llm("gemini"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldValidateOpenAiProvider() {
        StartupValidator validator = validator(telegram(), llm("openai"), deepSeek(), gemini(), remnawave(), openAi(), "openai");
        assertDoesNotThrow(() -> validator.run(null));
    }

    @Test
    void shouldThrowWhenBotTokenMissing() {
        TelegramProperties t = telegram();
        t.setBotToken(null);
        StartupValidator validator = validator(t, llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("TELEGRAM_BOT_TOKEN"));
    }

    @Test
    void shouldThrowWhenBotTokenBlank() {
        TelegramProperties t = telegram();
        t.setBotToken("  ");
        StartupValidator validator = validator(t, llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("TELEGRAM_BOT_TOKEN"));
    }

    @Test
    void shouldThrowWhenSupportGroupChatIdIsZero() {
        TelegramProperties t = telegram();
        t.setSupportGroupChatId(0);
        StartupValidator validator = validator(t, llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("TELEGRAM_SUPPORT_GROUP_CHAT_ID"));
    }

    @Test
    void shouldThrowWhenSupportGroupChatIdIsPositive() {
        TelegramProperties t = telegram();
        t.setSupportGroupChatId(123L);
        StartupValidator validator = validator(t, llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("отрицательным"));
    }

    @Test
    void shouldThrowForUnknownProvider() {
        StartupValidator validator = validator(telegram(), llm("unknown"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("Неизвестный LLM_PROVIDER"));
        assertTrue(ex.getMessage().contains("deepseek"));
    }

    @Test
    void shouldThrowForTypoInProvider() {
        StartupValidator validator = validator(telegram(), llm("openei"), deepSeek(), gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("Неизвестный LLM_PROVIDER"));
        assertTrue(ex.getMessage().contains("openai"));
    }

    @Test
    void shouldThrowWhenDeepseekApiKeyMissing() {
        DeepSeekProperties d = deepSeek();
        d.setApiKey(null);
        StartupValidator validator = validator(telegram(), llm("deepseek"), d, gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("DEEPSEEK_API_KEY"));
    }

    @Test
    void shouldThrowWhenDeepseekModelMissing() {
        DeepSeekProperties d = deepSeek();
        d.setModel(null);
        StartupValidator validator = validator(telegram(), llm("deepseek"), d, gemini(), remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("DEEPSEEK_MODEL"));
    }

    @Test
    void shouldThrowWhenOpenAiApiKeyMissing() {
        OpenAiProperties o = new OpenAiProperties();
        o.setModel("gpt-4");
        StartupValidator validator = validator(telegram(), llm("openai"), deepSeek(), gemini(), remnawave(), o, "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("OPENAI_API_KEY"));
    }

    @Test
    void shouldThrowWhenOpenAiApiKeyWrongFormat() {
        OpenAiProperties o = new OpenAiProperties();
        o.setApiKey("not-sk-prefix");
        o.setModel("gpt-4");
        StartupValidator validator = validator(telegram(), llm("openai"), deepSeek(), gemini(), remnawave(), o, "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("начинаться с 'sk-'"));
    }

    @Test
    void shouldThrowWhenOpenAiModelMissing() {
        OpenAiProperties o = openAi();
        o.setModel(null);
        StartupValidator validator = validator(telegram(), llm("openai"), deepSeek(), gemini(), remnawave(), o, "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("OPENAI_MODEL"));
    }

    @Test
    void shouldThrowWhenGeminiApiKeyMissing() {
        GeminiProperties g = new GeminiProperties();
        g.setModel("gemini-pro");
        StartupValidator validator = validator(telegram(), llm("gemini"), deepSeek(), g, remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("GEMINI_API_KEY"));
    }

    @Test
    void shouldThrowWhenGeminiModelMissing() {
        GeminiProperties g = gemini();
        g.setModel(null);
        StartupValidator validator = validator(telegram(), llm("gemini"), deepSeek(), g, remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("GEMINI_MODEL"));
    }

    @Test
    void shouldThrowWhenRemnawaveMcpUrlMissing() {
        RemnawaveMcpProperties r = new RemnawaveMcpProperties();
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), gemini(), r, openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("REMNAWAVE_MCP_URL"));
    }

    @Test
    void shouldThrowForUnknownEmbeddingProvider() {
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), gemini(), remnawave(), openAi(), "badprovider");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("Неизвестный EMBEDDING_PROVIDER"));
    }

    @Test
    void shouldThrowWhenEmbeddingOpenAiButNoApiKey() {
        OpenAiProperties o = new OpenAiProperties();
        o.setModel("gpt-4");
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), gemini(), remnawave(), o, "openai");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("OPENAI_API_KEY"));
    }

    @Test
    void shouldThrowWhenEmbeddingOpenAiButWrongKeyFormat() {
        OpenAiProperties o = new OpenAiProperties();
        o.setApiKey("wrong-key");
        o.setModel("gpt-4");
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), gemini(), remnawave(), o, "openai");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("sk-"));
    }

    @Test
    void shouldThrowWhenEmbeddingGeminiButNoApiKey() {
        GeminiProperties g = new GeminiProperties();
        g.setModel("gemini-pro");
        StartupValidator validator = validator(telegram(), llm("deepseek"), deepSeek(), g, remnawave(), openAi(), "gemini");
        var ex = assertThrows(IllegalStateException.class, () -> validator.run(null));
        assertTrue(ex.getMessage().contains("GEMINI_API_KEY"));
    }
}
