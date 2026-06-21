package com.vpnsupport.config;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

@Component
public class StartupValidator implements ApplicationRunner {

    private final TelegramProperties telegramProperties;
    private final LlmProperties llmProperties;
    private final DeepSeekProperties deepSeekProperties;
    private final GeminiProperties geminiProperties;
    private final RemnawaveMcpProperties remnawaveMcpProperties;

    public StartupValidator(TelegramProperties telegramProperties,
                            LlmProperties llmProperties,
                            DeepSeekProperties deepSeekProperties,
                            GeminiProperties geminiProperties,
                            RemnawaveMcpProperties remnawaveMcpProperties) {
        this.telegramProperties = telegramProperties;
        this.llmProperties = llmProperties;
        this.deepSeekProperties = deepSeekProperties;
        this.geminiProperties = geminiProperties;
        this.remnawaveMcpProperties = remnawaveMcpProperties;
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
        } else {
            throw new IllegalStateException("Unknown LLM provider: " + provider);
        }

        requireText(remnawaveMcpProperties.getBaseUrl(), "REMNAWAVE_BASE_URL");
        requireText(remnawaveMcpProperties.getApiToken(), "REMNAWAVE_API_TOKEN");
    }

    private static void requireText(String value, String name) {
        if (!StringUtils.hasText(value)) {
            throw new IllegalStateException(name + " is required");
        }
    }
}
