package com.vpnsupport.bot;

import java.util.regex.Pattern;

/**
 * Decides when a conversation needs a human tagged in the support topic.
 *
 * <p>The primary signal is the {@code [ESCALATE]} marker the model appends: it
 * has the whole conversation in view and the system prompt tells it exactly when
 * to use it. The keyword check below is only a safety net for a user explicitly
 * asking for a person.
 *
 * <p>Matching is anchored on word boundaries. The previous substring check on
 * "жив" fired on "живу в Германии" and "живот", pinging the admin for nothing.
 */
public final class EscalationPolicy {

    public static final String ESCALATE_MARKER = "[ESCALATE]";

    // UNICODE_CHARACTER_CLASS is required, not optional: without it \w and \b are
    // ASCII-only, so every boundary around a Cyrillic word silently fails to match.
    private static final Pattern ASKS_FOR_HUMAN = Pattern.compile(
            "\\b(оператор\\w*|человек\\w*|человеч\\w*|жив(ой|ого|ому|ым|ом))\\b",
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE | Pattern.UNICODE_CHARACTER_CLASS);

    /** True when the model asked for escalation. */
    public static boolean modelRequestedEscalation(String rawResponse) {
        return rawResponse != null && rawResponse.contains(ESCALATE_MARKER);
    }

    /** True when the user explicitly asked to talk to a person. */
    public static boolean userRequestsHuman(String userMessage) {
        return userMessage != null
                && !userMessage.isBlank()
                && ASKS_FOR_HUMAN.matcher(userMessage).find();
    }

    /** Removes the service marker before the text is shown to the user. */
    public static String stripMarker(String rawResponse) {
        return rawResponse == null ? "" : rawResponse.replace(ESCALATE_MARKER, "").trim();
    }

    private EscalationPolicy() {
    }
}
