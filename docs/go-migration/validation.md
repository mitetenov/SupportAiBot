# Local verification record

Date: 2026-10-20. Source baseline: `62f690fd92cd2c024045e94232a1dff2ce388f3e`.
The original checkout is unchanged. No production service was inspected or changed.

## Python reference

The frozen Python application/test files were tested from the isolated clone,
without `.env`, using the already installed Python 3.14.8 test environment:

```sh
python -m pytest -q --cov-report=json:/private/tmp/supportbot-python-coverage.json --cov-report=term
```

Result: **1719 passed**, **89.63% coverage**, 11.85 seconds. This establishes the
reference suite only, independently of the native implementation.

## Native unit, differential and integration acceptance

The aggregate native run uses `go test -race -count=1 -json ./...`, with a real
isolated PostgreSQL 17.11/pgvector 0.8.7 database and both unchanged native sibling
MCP executables. Tests use independent temporary schemas and local upstream API
fixtures. All database suites now use `TEST_DATABASE_URL` (the runtime/ticket tests
also retain their original `SUPPORTBOT_TEST_DATABASE_URL` test-only alias).

Reproduce the configured local acceptance (all endpoints and credentials below
belong only to the disposable local fixture setup):

```sh
PATH=/private/tmp/supportbot-go-toolchain/go/bin:$PATH \
GOCACHE=/private/tmp/supportbot-go-cache \
GOMODCACHE=/private/tmp/supportbot-go-modcache \
TEST_DATABASE_URL='postgres://parity:local-parity-only@127.0.0.1:64641/supportbot_parity?sslmode=disable' \
TEST_REMNAWAVE_MCP_BIN=/private/tmp/supportbot-remnawave-mcp \
TEST_BEDOLAGA_MCP_BIN=/private/tmp/supportbot-bedolaga-mcp \
./scripts/parity/test_go.sh -json
```

The final aggregate counts are recorded at final handoff below. Package-only
`internal/domain` has no tests and is not a skipped behavioral scenario.

Differential inputs captured from the Python baseline include:

- 196 provider request/capability/constructor fixtures, 78 response/parser fixtures,
  and 11 initial-conversation fixtures: **285 LLM fixture entries**.
- 14 configuration acceptance cases and 12 Russian rejection-boundary cases.
- 10 retrieval-query/catalog cases, one shared-prefix FAQ rendering case and two
  provider fingerprint cases.
- 26 Telegram formatting and three chunking cases.
- Five actual Python customer pipeline event traces and eight ticket question/status/null-normalization cases.

Real database checks cover Python-generated DDL and filled rows, exact
`faq_metadata(key,val)` compatibility, retained vectors of widths1536/2000,
legacy UTC/missing-column reconciliation, history chronology/restart/clear and
closed-pool fallback, nullable watermarks, ticket duplicate/restart/human takeover,
media retry after an answered-ticket restart, in-flight newer user messages,
shutdown cancellation, hybrid/FTS retrieval, atomic failed reindex and gap dedup.
True LRU eviction/concurrent duplicate insertion and retry/read failures are
independently exercised under the race detector.

Native MCP acceptance used existing sibling binaries from local source revisions
Remnawave `9b54b7950` and Bedolaga `c3ddbf2fa`. Discovery/schema snapshots matched:
Remnawave17 discovered /10 routed, Bedolaga8 discovered /8 routed. Calls traversed
the actual MCP HTTP compatibility wrappers into local backend fixtures; auth,
system-pinned identities and read-only boundaries were asserted. Modern MCP and
legacy2025-11-25 fallback are tested. These optional sibling-binary scenarios are
local acceptance; CI does not claim to build/test unrelated repositories.

Operational checks cover INFO/ERROR metadata, opt-in TRACE payloads and cap20,000, binary omission, secret redaction, token-counter preservation and correlation/request/operation context.

CI runs Go formatting, vet and race tests against a pgvector service, plus the
retained Python lint/type/reference tests. Local `gofmt`, `go vet`, `git diff
--check`, Ruff lint and formatting checks passed. No remote CI run is claimed.

## Native Docker acceptance

`docker build -t supportbot-go:parity .` builds a static native Go binary and an
Alpine image containing the binary, certificates and FAQ assets. The final image
was run with `python3 scripts/parity/docker_smoke.py` as an external test harness.
That harness is absent from the runtime image.

The complete container process used disposable PostgreSQL data and local fake
Telegram/MCP/OpenAI/embedding endpoints. It verified native health and the native
healthcheck subcommand, command registration, a customer help command, a model
answer, two durable history rows, successful MCP discovery, graceful exit0, and
absence of Python and `app/` in the runtime image. All temporary smoke database
and bot container resources were removed. After final acceptance, the isolated
test PostgreSQL container was also removed; the built image is retained. No
background API or sibling-MCP fixture process remains.

## Limits

The initial fixture-based validation below did not cover paid LLM calls, real Telegram delivery, production operator/device scenarios,
production data or permissions, external webhook registration, long-duration soak,
registry publication or deployment was verified. The native suite targets
contracts/state/races and representative flows; all1719 Python assertions have
not been translated individually. Per-area limits and narrow intended-behavior
corrections are explicit in [runtime-validation.md](runtime-validation.md).

## Final aggregate and handoff

Final uncached aggregate: **87 top-level tests + 340 subcases passed** across
11 tested packages; **0 failures, 0 skipped behavioral tests**. The domain package
contains shared types and has no independent tests. Both real database suites and
the two actual sibling MCP binaries were enabled in this run. Machine-readable
results are in [validation-results.json](validation-results.json).

Final native image ID:
`sha256:0a6b8096046b9d738d206512c12b1ed115e3421dc9039bd27ea91fbb79717926`.
The whole-container smoke on this image passed every check above after the final
logging/correlation changes. The local image was built/executed for linux/arm64;
linux/amd64 also compiled successfully. Neither architecture was published.
The attached managed worktree receives only source, locked dependencies, fixtures,
CI configuration and documentation. No secrets, compiled binaries, test caches or
private `.env` files are copied. A content manifest proves that the delivered
files match the tested tree; the original checkout and baseline remain intact.

## Later local validation (2026-10-20)

The user subsequently requested the real local stack startup and investigation of
a defective test-bot answer. That scope includes real configured startup embeddings,
a read-only inspection of the affected local conversation and two explicitly
authorized sanitized candidate LLM calls. See [local-go-stack.md](../local-go-stack.md)
and [sharing-response-incident.md](../sharing-response-incident.md). These are separate
from CI/mock coverage and do not claim production device or operator acceptance.

## Premerge revalidation (2026-10-20)

The final source was checked using the shared test launcher against a fresh
disposable database: **91 top-level tests and 380 subtests passed, zero failures
or skips**, with race detection and both native MCP fixture executables. The
launcher initializes pgvector in `public` before concurrent Go package tests; CI
uses the same launcher. Go vet passed. The unchanged Python suite passed again:
**1719 tests, 89.63% coverage**, with Ruff and mypy clean. See
[premerge-validation.json](premerge-validation.json). Optional actual sibling
binary acceptance remains local-only; CI does not expose those executables or
call paid providers/Telegram.
