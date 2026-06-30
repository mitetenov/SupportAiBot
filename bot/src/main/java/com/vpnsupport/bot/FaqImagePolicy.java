package com.vpnsupport.bot;

public final class FaqImagePolicy {

    public static boolean shouldAttachImages(String botResponse) {
        if (botResponse == null || botResponse.isBlank()) {
            return false;
        }
        String lower = botResponse.toLowerCase();
        return !indicatesUserNotFound(lower)
                && !indicatesPaymentOrTrafficIssue(lower)
                && !indicatesDeviceLimitIssue(lower);
    }

    private static boolean indicatesUserNotFound(String lower) {
        return lower.contains("не найден")
                || lower.contains("нет аккаунта")
                || lower.contains("не зарегистрирован")
                || lower.contains("отсутствует в системе");
    }

    private static boolean indicatesPaymentOrTrafficIssue(String lower) {
        if (lower.contains("триал") && (lower.contains("истёк") || lower.contains("истек")
                || lower.contains("законч") || lower.contains("исчерп"))) {
            return true;
        }
        if (lower.contains("подписк") && (lower.contains("истёк") || lower.contains("истек")
                || lower.contains("законч"))) {
            return true;
        }
        if (lower.contains("20 гб") || lower.contains("20гб")) {
            return true;
        }
        if ((lower.contains("оплат") || lower.contains("продли"))
                && !lower.contains("после оплат")) {
            return true;
        }
        return (lower.contains("лимит") || lower.contains("трафик"))
                && (lower.contains("исчерп") || lower.contains("превыш"))
                && !lower.contains("не превыш") && !lower.contains("не исчерп");
    }

    private static boolean indicatesDeviceLimitIssue(String lower) {
        if (lower.contains("hwid")) {
            return true;
        }
        if (lower.contains("сброс") && lower.contains("устройств")) {
            return true;
        }
        return lower.contains("5 устройств") || lower.contains("лимит устройств");
    }

    private FaqImagePolicy() {
    }
}
