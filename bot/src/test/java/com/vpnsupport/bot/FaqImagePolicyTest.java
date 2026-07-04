package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.junit.jupiter.params.provider.ValueSource;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class FaqImagePolicyTest {

    @ParameterizedTest
    @NullAndEmptySource
    void shouldNotAttachImagesWhenResponseNullOrBlank(String response) {
        assertFalse(FaqImagePolicy.shouldAttachImages(response));
    }

    @Test
    void shouldNotAttachImagesWhenUserNotFound() {
        assertFalse(FaqImagePolicy.shouldAttachImages("Пользователь не найден в системе"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Аккаунт не найден. Проверьте данные."));
        assertFalse(FaqImagePolicy.shouldAttachImages("У вас нет аккаунта в нашем сервисе"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Вы не зарегистрированы в системе"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Пользователь отсутствует в системе"));
    }

    @Test
    void shouldNotAttachImagesWhenPaymentIssue() {
        assertFalse(FaqImagePolicy.shouldAttachImages("Обратитесь в @PeipivoSalesBot для оплаты"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Ваш триал истёк, пополните баланс"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Триал закончился, нужна оплата"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Трафик 20 ГБ исчерпан"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Лимит 20гб израсходован"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Подписка истекла, продлите её"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Оплатите подписку для продолжения"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Нужно продлить подписку, она закончилась"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Лимит трафика превышен"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Трафик исчерпан полностью"));
    }

    @Test
    void shouldAttachImagesWhenPaymentButAfterPayment() {
        assertTrue(FaqImagePolicy.shouldAttachImages("После оплаты обновите подписку"));
    }

    @Test
    void shouldNotAttachImagesWhenTrafficNotExceeded() {
        assertTrue(FaqImagePolicy.shouldAttachImages("Ваш трафик не превышен"));
        assertTrue(FaqImagePolicy.shouldAttachImages("Лимит не исчерпан"));
    }

    @Test
    void shouldNotAttachImagesWhenDeviceLimitIssue() {
        assertFalse(FaqImagePolicy.shouldAttachImages("У вас привязано много устройств. Нужен HWID сброс."));
        assertFalse(FaqImagePolicy.shouldAttachImages("Сброс устройств требуется"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Достигнут лимит устройств на аккаунте"));
    }

    @Test
    void shouldAttachImagesForNormalConnectionResponses() {
        assertTrue(FaqImagePolicy.shouldAttachImages("Нажмите Обновить подписку в приложении Happ"));
        assertTrue(FaqImagePolicy.shouldAttachImages("Попробуйте пинг серверов и выберите с наименьшей задержкой"));
        assertTrue(FaqImagePolicy.shouldAttachImages("Вот инструкция по настройке приложения"));
        assertTrue(FaqImagePolicy.shouldAttachImages("Проверьте подключение к интернету"));
    }

    @Test
    void shouldAttachImagesForCommonResponses() {
        assertTrue(FaqImagePolicy.shouldAttachImages("Спасибо за обращение!"));
        assertTrue(FaqImagePolicy.shouldAttachImages("Проверяем ваш аккаунт..."));
        assertTrue(FaqImagePolicy.shouldAttachImages("Передаю запрос оператору"));
    }

    @Test
    void shouldAttachImagesWhenBotReferenceWithoutPaymentContext() {
        assertTrue(FaqImagePolicy.shouldAttachImages(
                "Скачайте приложение по инструкции из @PeipivoSalesBot и нажмите Обновить подписку"));
        assertTrue(FaqImagePolicy.shouldAttachImages(
                "Откройте @PeipivoSalesBot, вкладка Подключиться, и следуйте инструкции"));
    }

    @Test
    void caseInsensitiveMatching() {
        assertFalse(FaqImagePolicy.shouldAttachImages("ПОЛЬЗОВАТЕЛЬ НЕ НАЙДЕН в базе"));
        assertFalse(FaqImagePolicy.shouldAttachImages("Ваш ТРИАЛ ИСТЁК"));
        assertTrue(FaqImagePolicy.shouldAttachImages("НАЖМИТЕ ОБНОВИТЬ ПОДПИСКУ в Happ"));
    }
}
