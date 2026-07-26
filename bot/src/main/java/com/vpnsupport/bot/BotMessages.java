package com.vpnsupport.bot;

import org.springframework.context.MessageSource;
import org.springframework.stereotype.Component;

import java.util.Locale;

/**
 * Resolves user-facing text from {@code messages.properties}.
 *
 * <p>Every string the bot sends goes through here rather than being inlined at
 * the call site, so the wording lives in one reviewable file and can be changed
 * without touching control flow.
 */
@Component
public class BotMessages {

    private static final Locale LOCALE = Locale.of("ru");

    private final MessageSource messageSource;

    public BotMessages(MessageSource messageSource) {
        this.messageSource = messageSource;
    }

    public String get(String key, Object... args) {
        return messageSource.getMessage(key, args, LOCALE);
    }
}
