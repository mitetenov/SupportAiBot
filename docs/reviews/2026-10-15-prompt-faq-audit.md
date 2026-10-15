# Prompt and FAQ optimization audit

Baseline: master `eaad423`, including PR #112. The original prompt has 9,965
characters and the FAQ has 54 articles. Work is isolated from the original
checkout. Production services and customer records were not changed.

## Confirmed policy decisions

| Topic | Contradiction | Resolution confirmed by the owner |
| --- | --- | --- |
| Renewal | FAQ promised automatic renewal while the prompt required every period to be purchased separately | Optional renewal uses the internal balance, only for an active paid subscription. Users top up manually; the service does not automatically charge the bank card. It does not convert a trial or reactivate an expired subscription. |
| Renewal controls | Existing tests incorrectly rejected an autopay control | Bot: «Подписка → Автоплатеж → Включить». Cabinet: «Подписка → Автопродление». Disable in the same section; no invented button label. |
| Traffic | One article called all traffic unlimited, another described an exhausted trial allowance | Trial: 48 hours and 20 GB. Paid subscription: unlimited traffic. |
| Windows modes | Proxy was suggested for partial VPN use and for connection problems without OS restrictions | TUN/Proxy switching is a last step on Windows only, when enabling VPN makes all actual internet access fail. First update subscription and app, then verify that the selected server is active. An active status does not prove the absence of every server-side fault. |
| Gifts | One article required trial activation to create the recipient account | The recipient needs an account before purchase; trial activation is optional. |
| Telegram proxy | Independent proxy troubleshooting required a VPN subscription refresh | Proxy works independently; remove the VPN refresh step. |
| Linux | Linux appeared in the installation question but not supported-platform guidance | Linux is supported through the ready-made instruction on the subscription page; do not invent a client. |
| Incy controls | Generic controls and screenshot were described as Happ-specific | The owner confirmed that update and ping instructions apply to both Happ and Incy. |
| Cabinet labels | FAQ mixed «Баланс и платежи → Подписки» and «Баланс → Подписка» | The owner confirmed «Баланс → Подписка». |

Additional consistency fixes:

- Remove the obsolete three-dot copying instruction from the prompt. Preserve
  complete personal subscription URLs, including tokens, parameters and domains.
- Treat missing subscription import as a possible cause of an empty server list.
- An error or a profile lookup failure does not prove a missing Telegram link.
- Distinguish an attempted payment from a confirmed debit; never suggest paying
  again while a debit is unresolved. Retain the separate legacy Tribute article.
- Remove internal infrastructure terminology from payment instructions.
- Describe ping as response latency/reachability rather than download speed.
- Preserve the Google/Yandex diagnostic prerequisite and its limitations; do
  not repeat tests already completed in the conversation.
- Add a device-limit article using the existing tariff-dependent family-sharing
  policy, without inventing a numeric limit. Correct an outdated device-removal
  title in the retrieval benchmark, keeping the same intended article.

## What moved and what stays permanent

The prompt no longer repeats the tool inventory that is already supplied as
JSON schemas. It keeps routing, identity restrictions, factual-source rules,
mutation confirmation, payment triage, diagnostic prerequisites, escalation and
full-URL delivery. A test captures an actual Z.AI request and verifies that all
synthetic router tool descriptions and parameter schemas are still present.

Button locations and renewal navigation live in FAQ. The local FAQ catalog
supplements search with stable references for explicit subscription-link,
installation, button, n/a, renewal, trial and gift-recipient questions. It honors
rejected articles, removes duplicates and assigns no semantic similarity to
these direct references. This preserves essential instructions when retrieval
misses. It can add context; it is not a promise of fewer tokens in every case.

Identical long opening paragraphs shared by retrieved articles appear once,
with a reference from each applicable article. Indexed answers, conditions,
remaining steps, retrieval order and image metadata are preserved. Similar but
non-identical instructions are not combined.

The system-pinned current UTC date follows untrusted FAQ context, before the
pinned Telegram identity. This prevents an expired historical purchase from
being mistaken for a current paid period.

## Evaluation method and corrections

`prompt_quality.py` runs the production LLM client against **synthetic MCP
results**, with `glm-5.3-flash`, the same reasoning setting and no provider
fallback. It records every HTTP call, request segment sizes, latency and actual
input/output/cache usage. Missing cache data is unknown, not zero. Full synthetic
reports are retained locally; credentials and raw HTTP requests are not recorded.
These scenarios substitute FAQ candidates, so they do not prove vector recall.

`prompt_retrieval_eval.py` separately runs the real pgvector/FTS/fallback path in
a disposable local PostgreSQL database with OpenAI `text-embedding-3-small`.
Its 35 questions are hand-written synthetic paraphrases, not production dialog
exports. Identical text vectors are cached across revisions. The checked-in
query set and CLI make the comparison reproducible. The local database guard
rejects production hostnames and ordinary application database names.

The initial evaluator itself had these flaws, corrected with positive and
negative regression probes:

- Its supposedly active purchase had already expired. Date placeholders now
  make the purchase yesterday and its end 29 days in the future.
- The deposit case did not say whether an existing paid subscription was still
  active. It now explicitly says expired and autopay disabled, as required for
  the separate-purchase expectation.
- “Это не означает/не значит, что деньги не дошли” was counted as a claim that
  money had not arrived. Direct disclaimers are recognized; a later affirmative
  absence statement still fails.
- Explaining the confirmed restricted renewal rule was counted as a promise to
  reactivate an expired subscription. The scoped rule is accepted; an added
  promise of automatic reactivation still fails.
- “На каком именно шаге” was missed; asking whether a debit happened before an
  already known bank error was treated as repeating the known error question.
- New policy probes now distinguish general questions from personal lookups,
  accept the spelling “не обязательно”, and check actual mode-switch advice
  rather than a statement that switching is not needed yet. Server-list fixtures
  omit status so detailed server verification is actually necessary.

The acceptance conditions for URL integrity, identity, actual operator handoff,
payment order and forbidden tool access remain enforced. Initial failures and
policy changes are not silently counted as successful optimization results.

## Measurements

Final results and remaining model limitations are recorded below after the
three-run comparison. Character reduction alone is not a monetary saving;
cached and uncached token counts must be considered separately.

Production deployment and its FAQ reindex are separate steps. The normal startup
initializer detects changed FAQ content and replaces the index atomically; no
production reindex was run during this work.

### Recorded results (2026-10-15)

The compact evidence report is [prompt-faq-results.json](2026-10-15-prompt-faq-results.json).
Both sides were scored with the same corrected evaluator; original local HTTP
reports were preserved. Test identities are pinned by the synthetic router.

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Static prompt characters | 9,965 | 9,258 (−7.1%) |
| Common behavioral scenarios, 16 × 3 | 41/48 | 46/48 |
| Newly confirmed policy scenarios, 16 × 3 | not comparable: old contradictory policy | 48/48 |
| Required FAQ present, 32 positive queries | 25/32 | 31/32 |
| Correct first FAQ, 32 positive queries | 25/32 | 27/32 |
| Unrelated controls with no FAQ, 3 queries | 3/3 | 3/3 |
| Total formatted FAQ context, 35 queries | 93,719 chars | 85,917 chars (−8.3%) |

There were no lost positive retrieval outcomes. The remaining retrieval miss is
“Приложение показывает четыре тысячи миллисекунд задержки”; it also failed in the
baseline. This small synthetic set is a regression check, not a statistical
estimate of production support quality.

The full behavioral comparison used the 9,004-character candidate. Its two real
failures were a repeated internal term (“панели”) and a missing handoff marker.
The final 9,258-character revision adds a direct-action example and an explicit
handoff closing line, and adopts the owner's final cabinet labels. A targeted
repeat on this exact prompt/FAQ revision passed **12/12**: four runs each for
public payment guidance with internal names in the question, general payment
instructions, and a confirmed purchase/access discrepancy requiring handoff.
The full 96-case run was not repeated after these targeted changes. Passing
samples do not guarantee that a stochastic model will never repeat a mistake.

For the common 48-case run, input tokens were **254,216 → 267,546** (+5.2%), with
**75 → 83** API calls. The candidate performs payment checks that the baseline
sometimes skipped, so this is not a net input-token saving. Output tokens were
7,509 → 7,690; reported cached input was 208,576 → 242,560; uncached input was
45,640 → 24,986. Cache warmth and different tool-call counts prevent a clean
monetary comparison. The accepted optimization is smaller permanent/reference
context with better measured behavior and clarified policies, not a promise of
lower total cost for every conversation.

Local validation includes the complete pytest suite, coverage gate, Ruff and
mypy. CI and any subsequent deployment are reported separately in the PR.
