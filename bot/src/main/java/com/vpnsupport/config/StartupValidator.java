package com.vpnsupport.config;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

@Component
public class StartupValidator implements ApplicationRunner {

    private final TelegramProperties telegramProperties;
    private final LlmProperties llmProperties;
    private final DeepSeekProperties deepSeekProperties;
    private final GeminiProperties geminiProperties;
    private final RemnawaveMcpProperties remnawaveMcpProperties;
    private final OpenAiProperties openAiProperties;
    private final GroqProperties groqProperties;

    @Autowired
    public StartupValidator(TelegramProperties telegramProperties,
                            LlmProperties llmProperties,
                            DeepSeekProperties deepSeekProperties,
                            GeminiProperties geminiProperties,
                            RemnawaveMcpProperties remnawaveMcpProperties,
                            OpenAiProperties openAiProperties,
                            GroqProperties groqProperties) {
        this.telegramProperties = telegramProperties;
        this.llmProperties = llmProperties;
        this.deepSeekProperties = deepSeekProperties;
        this.geminiProperties = geminiProperties;
        this.remnawaveMcpProperties = remnawaveMcpProperties;
        this.openAiProperties = openAiProperties;
        this.groqProperties = groqProperties;
    }

    public StartupValidator(TelegramProperties telegramProperties,
                            LlmProperties llmProperties,
                            DeepSeekProperties deepSeekProperties,
                            GeminiProperties geminiProperties,
                            RemnawaveMcpProperties remnawaveMcpProperties,
                            OpenAiProperties openAiProperties) {
        this(telegramProperties, llmProperties, deepSeekProperties, geminiProperties,
                remnawaveMcpProperties, openAiProperties, new GroqProperties());
    }

    @Override
    public void run(ApplicationArguments args) {
        requireText(telegramProperties.getBotToken(), "TELEGRAM_BOT_TOKEN");
        if (telegramProperties.getSupportGroupChatId() == 0) {
            throw new IllegalStateException("TELEGRAM_SUPPORT_GROUP_CHAT_ID is required");
        }

        String provider = llmProperties.getProvider();
        if ("deepseek".equalsIgnoreCase(provider)) {
            requireText(deepSeekProperties.getApiKey(), "DEEPSEEK_API_KEY");
            requireText(deepSeekProperties.getModel(), "DEEPSEEK_MODEL");
        } else if ("gemini".equalsIgnoreCase(provider)) {
            requireText(geminiProperties.getApiKey(), "GEMINI_API_KEY");
            requireText(geminiProperties.getModel(), "GEMINI_MODEL");
        } else if ("openai".equalsIgnoreCase(provider)) {
            requireText(openAiProperties.getApiKey(), "OPENAI_API_KEY");
            requireText(openAiProperties.getModel(), "OPENAI_MODEL");
        } else if ("groq".equalsIgnoreCase(provider)) {
            requireText(groqProperties.getApiKey(), "GROQ_API_KEY");
            requireText(groqProperties.getModel(), "GROQ_MODEL");
        } else {
            throw new IllegalStateException("Unknown LLM provider: " + provider);
        }

        // In containerized mode (MCP_URL set), the MCP server container
        // handles the Remnawave API connection — no need for base-url here.
        if (remnawaveMcpProperties.getUrl() == null || remnawaveMcpProperties.getUrl().isBlank()) {
            requireText(remnawaveMcpProperties.getBaseUrl(), "REMNAWAVE_BASE_URL");
            requireText(remnawaveMcpProperties.getApiToken(), "REMNAWAVE_API_TOKEN");
        }
    }

    private static void requireText(String value, String name) {
        if (!StringUtils.hasText(value)) {
            throw new IllegalStateException(name + " is required");
        }
    }
}
