package com.vpnsupport.llm;

import com.vpnsupport.rag.FaqEmbeddingService;

/**
 * A model reply together with the FAQ retrieval that produced it.
 *
 * <p>Carrying the retrieval alongside the text lets the caller attribute a
 * knowledge gap to the exact entries the model was shown, without re-running the
 * search or reading it back out of thread-local state.
 *
 * @param text       the raw model output, {@code [ESCALATE]} marker included
 * @param faqContext the FAQ entries placed in front of the model
 */
public record LlmReply(String text, FaqEmbeddingService.FaqContext faqContext) {

    public LlmReply(String text) {
        this(text, FaqEmbeddingService.FaqContext.EMPTY);
    }
}
