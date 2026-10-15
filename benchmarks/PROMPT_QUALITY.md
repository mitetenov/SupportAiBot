# Prompt and FAQ quality checks

Run local tests first. Live runs send the system prompt, FAQ and synthetic
conversations/tool results to Z.AI and consume tokens. Obtain approval for the
scope and run count before using `--confirm-external-api`. No production MCP
server is contacted. The evaluator explicitly selects `glm-5.3-flash` and
disables fallback, regardless of the default provider in the env file.

```sh
python -m benchmarks.prompt_quality --env-file /path/to/.env \
  --label candidate-core --report /tmp/candidate-core.json --runs 3 \
  --confirm-external-api
python -m benchmarks.prompt_quality --env-file /path/to/.env \
  --cases-file benchmarks/policy_behavior_cases.json \
  --label candidate-policies --report /tmp/candidate-policies.json --runs 3 \
  --confirm-external-api
```

The report retains each synthetic answer, tool sequence, violations and usage
for all API calls. It saves progress after each scenario and refuses to overwrite
an existing report. Missing usage/cache values remain null. A nonzero exit code
means at least one scenario failed, not that the report is lost. Read the actual
answers and tool sequences; wording-based assertions can produce false positives.

For a baseline, run the same evaluator and resolved case fixture against the
baseline checkout on `PYTHONPATH`. Use the exact same scenarios and model
settings. Relative date fixtures must be resolved on the same date. Preserve
both reports and re-score both with the same corrected scorer if a scorer bug
is found. Do not change expected outcomes solely to improve a score.

Retrieval is a separate check and sends FAQ/query text to OpenAI embeddings.
Create a disposable local pgvector database named `prompt_opt*`. The command
replaces its FAQ rows; never point it at an application database. The env file's
database setting is not used.

```sh
python -m benchmarks.prompt_retrieval_eval --env-file /path/to/.env \
  --baseline-faq /tmp/baseline-faq.json \
  --database-url postgresql+asyncpg://local:synthetic@127.0.0.1:55493/prompt_opt \
  --report /tmp/retrieval-comparison.json --confirm-external-api
```

The shipped set has 32 positive queries and 3 unrelated controls. `correct`
includes controls that return nothing; `top1` counts only positive queries.
Compare recall, rank, new regressions and context length independently. Direct
catalog additions are distinguishable from raw search hits in each report.
Delete the disposable database/container when finished.
