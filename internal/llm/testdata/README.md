The fixtures were captured without network calls from Python baseline
`62f690fd92cd2c024045e94232a1dff2ce388f3e` on 2026-10-20.

* `python_requests.json`: 196 combinations of six adapters, model families and
  all seven configured reasoning levels. Includes constructor rejection,
  capabilities and exact request JSON for a deterministic tool schema.
* `python_responses.json`: 78 provider/parser cases covering valid/empty/error
  replies, terminal finish reasons, tool arguments, reasoning, Gemini thought
  signatures and OpenAI opaque reasoning output replay.

Tests compare semantic JSON; JSON strings carrying tool arguments are decoded
before comparison so JSON whitespace does not count as a protocol change.
* `python_conversations.json`: 11 initial conversation snapshots across six
  adapters, stored history, trusted dynamic identity, images and native PDF.
  The changing UTC date is normalized before comparison.

No real provider requests or real customer data were used.
