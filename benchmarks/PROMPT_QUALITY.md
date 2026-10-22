# Prompt, behavior and FAQ quality checks

All evaluators use the production Go LLM clients, trusted MCP router and FAQ
formatter. MCP owners and conversation identities are synthetic. Local tests
never contact providers. Live commands require `--confirm-external-api` and send
synthetic conversation/FAQ content to the configured provider, consuming tokens.

```sh
go test ./internal/eval ./internal/llm ./internal/rag ./internal/mcp

go run ./cmd/benchmarks behavior --env-file /path/to/.env \
  --runs 3 --threshold 0.8 --report /tmp/behavior.json --confirm-external-api

go run ./cmd/benchmarks quality --env-file /path/to/.env \
  --label candidate-core --report /tmp/candidate-core.json --runs 3 \
  --confirm-external-api

go run ./cmd/benchmarks quality --env-file /path/to/.env \
  --cases-file benchmarks/policy_behavior_cases.json \
  --label candidate-policies --report /tmp/candidate-policies.json --runs 3 \
  --confirm-external-api
```

`behavior` uses the configured provider and fallback chain. `quality` explicitly
selects Z.AI `glm-5.3-flash` and disables fallback, preserving the original
comparison settings. All six providers are supported. `--case NAME` is repeatable;
`--concurrency` accepts 1..4. Diagnosis scenarios are in
`benchmarks/diagnosis_behavior_cases.json` and use the same `--cases-file` flag.

Reports reserve a new path before evaluation and refuse to overwrite files.
Each completed case saves its synthetic answer, tool sequence, violations,
latency, request sizes and token usage. Missing usage/cache values remain null.
Observer records contain no authorization headers, URLs, request text or raw
errors. Provider failures remain failed cases in the report. The behavior
command fails below the per-case threshold; quality fails if any case fails.
Relative dates are resolved in UTC and saved with the resolved case fixtures.
Use the same resolved fixtures, model and settings for comparisons. Read answers
and tool sequences: the heuristic scorer is not a semantic judge.

## Retrieval comparison

Use a disposable loopback PostgreSQL/pgvector database named `prompt_opt*`.
The env file's application database is never used. Comparison rebuilds its FAQ
index and reuses identical embeddings across both revisions for fairness.

```sh
go run ./cmd/benchmarks compare --env-file /path/to/.env \
  --baseline-faq /tmp/baseline-faq.json \
  --database-url 'postgres://local:synthetic@127.0.0.1:55493/prompt_opt' \
  --report /tmp/retrieval-comparison.json --confirm-external-api

go run ./cmd/benchmarks retrieval --env-file /path/to/.env \
  --database-url 'postgres://local:synthetic@127.0.0.1:55493/prompt_opt' \
  --reindex --detail-at 0.45 --detail-mode append \
  --report /tmp/retrieval-grid.json --confirm-external-api
```

`compare` uses OpenAI `text-embedding-3-small` and
`benchmarks/prompt_retrieval_cases.json` by default, and keeps raw search results
separate from candidate catalog additions. `retrieval` uses the configured
embedding provider, `benchmarks/retrieval_queries.json`, all twelve similarity /
limit pairs and three independent fallback modes: `append`, `compete`, `off`.
It records held-out scores separately from keyword-tuned queries and controls.
Its default row uses production similarity 0.45 and limit 3; real pgvector tests
compare the evaluator's results with the production retrieval path.
`--cases` and `--faq` select other fixtures. Without `--reindex`, the grid uses
existing vectors in the disposable database. Remove the fixture database after
measurement. Compare recall, rank, regressions and context length independently.
