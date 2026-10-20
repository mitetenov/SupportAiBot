# Subscription sharing response incident — 2026-10-20

The malformed reply was already stored as the assistant response before Telegram formatting. The six persisted messages alternate correctly and contain no unrelated conversation history. For the affected turn the configured provider was `zai`, model `glm-5.3-flash`, effective reasoning effort `high`: one HTTP200 attempt, no tools and no fallback. The parser reads visible message content separately from reasoning; controlled parser fixtures verify that separation. Original raw provider JSON was not captured at INFO, so its exact historical fields cannot be recovered.

The concrete retrieval defect was inherited from the previous implementation: the follow-up recognizer accepted `её` but missed the common spelling `ее`. The sharing question consequently lost the preceding subscription-link context. Its typo in the recipient word also missed the friend expansion. The persisted knowledge-gap record reports LOW_SIMILARITY, three FAQ results and a connection-troubleshooting article as the best match (maximum similarity 0.6437962168). Re-running the original hybrid SQL with the saved query embedding against the unchanged local index returns zero primary-query matches. The full historical candidate list was not logged.

The existing FAQ already answers the real question: **«Как поделиться подпиской с членом семьи?»** instructs customers how to connect another person's device, subject to their tariff's device limit, with shared traffic and expiry. It does not impose a blanket sharing ban or claim account blocking. The reply invented those restrictions and included unrelated text.

## Bounded correction

- Normalize `ё/е` and recognize anaphoric follow-ups so the relevant prior user question remains in retrieval context.
- Include the existing sharing article deterministically for subscription-sharing/permission intent. Referral links, payment for a friend, unrelated connection failures and explicit access to someone else's account/link without consent remain separate intents. This is article selection, not a new permission or refusal policy.
- Give the legacy article an in-memory stable anchor. FAQ JSON and its persisted fingerprint remain unchanged; restarting retained all 55 embeddings without reindexing.
- Require claims about restrictions/sanctions to be grounded in applicable FAQ or checked tool facts. Earlier assistant statements are not evidence; contradictory previous answers should be acknowledged and corrected. No keyword ban or canned denial was added.

Targeted uncached race validation passed **44 top-level tests and 364 subtests, zero failures/skips**, using isolated real PostgreSQL and actual MCP fixture binaries. Two bounded live candidate calls to the configured provider returned HTTP200: the original question received the verified sharing instructions, and a follow-up after the bad assistant statement received an explicit correction. The calls used sanitized history, verified FAQ, no tools and no Telegram sends or history writes. These checks establish the bounded repair, not a guarantee against every future model error.

Only the local support-bot container was rebuilt/restarted. Both MCPs and the database kept running. The new bot is healthy, connects to 17 Remnawave and 8 Bedolaga tools, retains 55 FAQ vectors, and has no observed polling 409 conflict. All six original history row IDs and contents were preserved, including the faulty answer: no silent deletion or rewrite occurred. Future responses are instructed to correct it using verified FAQ.

Exact test counts, replay texts without personal subscription URLs, image ID and post-restart evidence are in [the results JSON](sharing-response-incident-results.json). No remote service configuration was changed and no artificial customer/group messages were sent.
