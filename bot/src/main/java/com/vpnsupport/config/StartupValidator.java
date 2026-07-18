package com.vpnsupport.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.Set;

@Component
public class StartupValidator implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(StartupValidator.class);
    private static final List<String> VALID_LLM_PROVIDERS = List.of("deepseek", "gemini", "openai");
    private static final List<String> VALID_EMBEDDING_PROVIDERS = List.of("gemini", "openai");

    private final TelegramProperties telegramProperties;
    private final LlmProperties llmProperties;
    private final DeepSeekProperties deepSeekProperties;
    private final GeminiProperties geminiProperties;
    private final RemnawaveMcpProperties remnawaveMcpProperties;
    private final OpenAiProperties openAiProperties;
    private final String embeddingProvider;

    public StartupValidator(TelegramProperties telegramProperties,
                            LlmProperties llmProperties,
                            DeepSeekProperties deepSeekProperties,
                            GeminiProperties geminiProperties,
                            RemnawaveMcpProperties remnawaveMcpProperties,
                            OpenAiProperties openAiProperties,
                            @Value("${embedding.provider}") String embeddingProvider) {
        this.telegramProperties = telegramProperties;
        this.llmProperties = llmProperties;
        this.deepSeekProperties = deepSeekProperties;
        this.geminiProperties = geminiProperties;
        this.remnawaveMcpProperties = remnawaveMcpProperties;
        this.openAiProperties = openAiProperties;
        this.embeddingProvider = embeddingProvider;
    }

    @Override
    public void run(ApplicationArguments args) {
        validateTelegram();
        validateLlmProvider();
        validateEmbeddingProvider();
        validateRemnawave();
        log.info("Startup validation passed");
    }

    private void validateTelegram() {
        requireText(telegramProperties.getBotToken(),
                "TELEGRAM_BOT_TOKEN не задан. Получите токен у @BotFather и добавьте в .env: TELEGRAM_BOT_TOKEN=<токен>");

        long chatId = telegramProperties.getSupportGroupChatId();
        if (chatId == 0) {
            throw new IllegalStateException(
                    "TELEGRAM_SUPPORT_GROUP_CHAT_ID не задан. "
                    + "Создайте супергруппу в Telegram, включите Topics (форум) "
                    + "и укажите её ID в .env: TELEGRAM_SUPPORT_GROUP_CHAT_ID=-100XXXXXXXXXX");
        }
        if (chatId > 0) {
            throw new IllegalStateException(
                    "TELEGRAM_SUPPORT_GROUP_CHAT_ID должен быть отрицательным числом (ID супергруппы). "
                    + "Сейчас задан положительный ID: " + chatId + ". "
                    + "Убедитесь, что группа преобразована в супергруппу (включены Topics/форум). "
                    + "ID супергруппы начинается с -100, например: -1001234567890");
        }

        Set<Long> adminIds = telegramProperties.getSupportAdminTelegramIds();
        if (adminIds.isEmpty()) {
            log.warn("TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS не задан — "
                     + "уведомления об ошибках и запросы оператора не будут отправляться администраторам. "
                     + "Добавьте в .env: TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS=123456789,987654321");
        }
    }

    private void validateLlmProvider() {
        String provider = llmProperties.getProvider();
        if (!StringUtils.hasText(provider)) {
            throw new IllegalStateException(
                    "LLM_PROVIDER не задан. Укажите один из: "
                    + String.join(", ", VALID_LLM_PROVIDERS)
                    + "\nПример: LLM_PROVIDER=openai");
        }

        String normalized = provider.trim().toLowerCase();
        if (!VALID_LLM_PROVIDERS.contains(normalized)) {
            throw new IllegalStateException(
                    "Неизвестный LLM_PROVIDER: '" + provider + "'. "
                    + "Допустимые значения: " + String.join(", ", VALID_LLM_PROVIDERS)
                    + "\nПроверьте опечатку в .env: LLM_PROVIDER=" + provider);
        }

        switch (normalized) {
            case "deepseek":
                requireText(deepSeekProperties.getApiKey(),
                        "DEEPSEEK_API_KEY не задан. Получите ключ на https://platform.deepseek.com/api_keys "
                        + "и добавьте в .env: DEEPSEEK_API_KEY=sk-...");
                requireText(deepSeekProperties.getModel(),
                        "DEEPSEEK_MODEL не задан. Укажите модель, например: DEEPSEEK_MODEL=deepseek-chat");
                break;
            case "gemini":
                requireText(geminiProperties.getApiKey(),
                        "GEMINI_API_KEY не задан. Получите ключ в Google AI Studio: "
                        + "https://aistudio.google.com/apikey и добавьте в .env: GEMINI_API_KEY=...");
                requireText(geminiProperties.getModel(),
                        "GEMINI_MODEL не задан. Укажите модель, например: GEMINI_MODEL=gemini-2.5-flash");
                break;
            case "openai":
                validateOpenAiApiKey();
                requireText(openAiProperties.getModel(),
                        "OPENAI_MODEL не задан. Укажите модель, например: OPENAI_MODEL=gpt-5.4-mini");
                break;
        }
    }

    private void validateOpenAiApiKey() {
        String key = openAiProperties.getApiKey();
        requireText(key,
                "OPENAI_API_KEY не задан. Получите ключ на https://platform.openai.com/api-keys "
                + "и добавьте в .env: OPENAI_API_KEY=sk-...");
        if (!key.trim().startsWith("sk-")) {
            throw new IllegalStateException(
                    "OPENAI_API_KEY должен начинаться с 'sk-'. "
                    + "Проверьте, что вы не перепутали его с Telegram-токеном или ключом другого провайдера. "
                    + "Текущее значение начинается с: '"
                    + key.trim().substring(0, Math.min(5, key.trim().length())) + "...'");
        }
    }

    private void validateEmbeddingProvider() {
        String normalized = embeddingProvider.trim().toLowerCase();
        if (!VALID_EMBEDDING_PROVIDERS.contains(normalized)) {
            throw new IllegalStateException(
                    "Неизвестный EMBEDDING_PROVIDER: '" + embeddingProvider + "'. "
                    + "Допустимые значения: " + String.join(", ", VALID_EMBEDDING_PROVIDERS)
                    + "\nПроверьте .env: EMBEDDING_PROVIDER=" + embeddingProvider);
        }

        if ("openai".equals(normalized)) {
            String key = openAiProperties.getApiKey();
            if (!StringUtils.hasText(key)) {
                throw new IllegalStateException(
                        "EMBEDDING_PROVIDER=openai, но OPENAI_API_KEY не задан. "
                        + "Добавьте в .env: OPENAI_API_KEY=sk-... "
                        + "или смените провайдера: EMBEDDING_PROVIDER=gemini");
            }
            if (!key.trim().startsWith("sk-")) {
                throw new IllegalStateException(
                        "EMBEDDING_PROVIDER=openai, но OPENAI_API_KEY имеет неверный формат "
                        + "(должен начинаться с 'sk-'). Проверьте .env: OPENAI_API_KEY");
            }
        }

        if ("gemini".equals(normalized)) {
            requireText(geminiProperties.getApiKey(),
                    "EMBEDDING_PROVIDER=gemini, но GEMINI_API_KEY не задан. "
                    + "Добавьте в .env: GEMINI_API_KEY=... "
                    + "или смените провайдера: EMBEDDING_PROVIDER=openai");
        }
    }

    private void validateRemnawave() {
        requireText(remnawaveMcpProperties.getUrl(),
                "REMNAWAVE_MCP_URL не задан. Укажите URL MCP-сервера Remnawave, "
                + "например: REMNAWAVE_MCP_URL=http://mcp-remnawave:3100");
    }

    private static void requireText(String value, String message) {
        if (!StringUtils.hasText(value)) {
            throw new IllegalStateException(message);
        }
    }
}
