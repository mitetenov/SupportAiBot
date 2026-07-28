package com.vpnsupport.bot;

import java.util.List;
import java.util.Optional;

/**
 * Outcome of sending a message through Telegram.
 *
 * <p>Callers used to get {@code void} and a log line, so a message that never
 * reached the user looked exactly like one that did. Two things need this: the
 * support topic has to be told when delivery failed, and edit propagation has to
 * know which message ID to edit later.
 *
 * @param messageIds IDs of the messages actually sent, in order; a long text is
 *                   split across several
 */
public record Delivery(boolean delivered, List<Integer> messageIds) {

    private static final Delivery FAILED = new Delivery(false, List.of());

    public static Delivery failed() {
        return FAILED;
    }

    public static Delivery of(List<Integer> messageIds) {
        return new Delivery(!messageIds.isEmpty(), List.copyOf(messageIds));
    }

    /**
     * The single message this send produced, if it produced exactly one.
     *
     * <p>Empty when the text was long enough to be split: an edit cannot be
     * mapped onto several messages, so those exchanges simply do not
     * participate in edit propagation.
     */
    public Optional<Integer> singleMessageId() {
        return messageIds.size() == 1 ? Optional.of(messageIds.get(0)) : Optional.empty();
    }
}
