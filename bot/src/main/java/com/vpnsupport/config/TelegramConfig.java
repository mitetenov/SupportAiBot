package com.vpnsupport.config;

import com.pengrad.telegrambot.TelegramBot;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class TelegramConfig {

    @Bean
    public TelegramBot telegramBot(TelegramProperties properties) {
        return new TelegramBot(properties.getBotToken());
    }
}
