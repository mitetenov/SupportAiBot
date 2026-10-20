# Native Go runtime and compatibility

The default Docker image and Compose healthcheck execute `/app/supportbot`.
Python is absent from that image. The retained `app/`, `tests/`, `pyproject.toml`
and `uv.lock` are the frozen behavioral reference and optional validation tools.
The two MCP services remain independent and unchanged by this bot migration.

## Build and local checks

Go 1.26 or later is required. The tested local toolchain is Go 1.27.3.

```sh
go build -o supportbot ./cmd/supportbot
./supportbot
./supportbot healthcheck
go vet ./...
go test -race ./...
```

The binary reads `.env`, then lets existing environment variables take precedence.
All Python environment names and defaults are retained. Startup validation errors
name the setting without echoing its credential. `BOT_LOG_LEVEL` accepts TRACE,
INFO and ERROR; `LOG_LEVEL` is retained as an optional legacy fallback only when
BOT_LOG_LEVEL is absent. `TELEGRAM_API_BASE_URL` is an additional optional setting
for a self-hosted Telegram Bot API or local fixture server; its empty default uses
`https://api.telegram.org`. It does not change normal Telegram routing.

For real local database tests, supply a disposable PostgreSQL 17 database with
pgvector, the host `psql` client and permission to create/drop test schemas:

```sh
TEST_DATABASE_URL='postgres://test:test@127.0.0.1:5432/test?sslmode=disable' ./scripts/parity/test_go.sh
```

The exact local PostgreSQL fixture used for acceptance can be recreated with:

```sh
docker run -d --name supportbot-go-parity-pg \
  -p 127.0.0.1:64641:5432 \
  -e POSTGRES_USER=parity -e POSTGRES_PASSWORD=local-parity-only \
  -e POSTGRES_DB=supportbot_parity \
  --health-cmd='pg_isready -U parity -d supportbot_parity' \
  pgvector/pgvector:pg17
```

Wait for `docker inspect` to report this fixture healthy before running the
all-acceptance command in validation.md. This task removed that disposable
container after verification and kept the local image.

The shared test launcher creates the `vector` extension in `public` before Go starts package tests concurrently. This initialization is required on a fresh database; creating the extension from individual temporary schemas can race. Each integration test then creates its own schema and removes only that schema.
Optional acceptance tests can start the existing native sibling MCP executables
using `TEST_REMNAWAVE_MCP_BIN` and `TEST_BEDOLAGA_MCP_BIN`. Their upstream APIs are
local fixtures; the sibling services' source code is not changed.

The Docker smoke harness in `scripts/parity/docker_smoke.py` is a test utility,
not a runtime dependency. It expects a local fixture PostgreSQL container named
`supportbot-go-parity-pg` with the synthetic credentials declared in that script,
and a built image `supportbot-go:parity`. It creates/removes its own disposable
database and container and runs only local fake Telegram/MCP/LLM/embedding APIs.

## Support diagnostics

`BOT_LOG_LEVEL=INFO` records operational metadata: component/operation,
provider/model/effective reasoning, attempts, status/duration, fallbacks, routing,
reconnects, startup/reindex and shutdown. `ERROR` restricts output to failures.
`TRACE` explicitly enables sanitized message, request/response, tool and retrieval
payloads. Image/PDF binary data is omitted, credentials are redacted, and TRACE
payloads are capped at 20,000 characters. Correlation/request IDs connect customer
or ticket processing to downstream provider/API activity. Enabling TRACE therefore
also makes conversational text available to the operator collecting those logs.

## Database compatibility

The Go service reuses the existing PostgreSQL tables and keys, including
`faq_metadata(key,val)`, signed 64-bit conversation IDs, nullable `active_ticket_id`
and human timestamps, token counts, chat chronology and ticket/media watermarks.
Startup is idempotent. Old naive timestamps are interpreted as UTC. Matching
vector dimensions preserve existing vectors and indexes. Changing dimensions
invalidates incompatible embeddings; changing model/base URL also changes the
index fingerprint, which deliberately preserves the Python provider class names.

New FAQ embeddings are fully prepared before the transaction that replaces the
active rows. An incomplete batch leaves the old rows and fingerprint intact.
If an embedding model changes and replacement fails, Go serves safe full-text
retrieval from retained rows while disabling vector queries against the old model.
This availability detail differs from Python's unready state after that failure;
source rows remain intact and the next startup retries the replacement.

No distributed queue or multiple-replica semantics have been added. Coalescing,
locks and scheduled tasks remain in-process; ticket progress remains durable in
PostgreSQL. Run one active bot instance against a given Telegram token and state.

## Deployment and rollback procedure

These are operator instructions; this task has not deployed anything.

1. Record the currently deployed bot image digest, MCP image tags and configuration,
   and back up the PostgreSQL database. A local source SHA is not a verified
   production baseline.
2. Build/publish a distinct Go image tag only through the existing tested release
   process. Preserve the previous Python image and configuration for rollback.
3. Stop the old bot before starting the Go image to avoid simultaneous polling or
   duplicate ticket writers. Retain the PostgreSQL volume and MCP image versions.
4. Start the Go image and verify native health, personal and cabinet conversations,
   attachment delivery, operator handover and real provider access before treating
   rollout as successful. Compose allows a 30-second stop grace period for drains.
5. To roll back, stop Go and restore the recorded Python bot image/configuration;
   leave MCP services and the database volume intact. The schema remains readable
   by the Python baseline. If changing embedding model/dimension, restore the
   matching embedding configuration too; compatible FAQ content is reindexed on
   startup when needed.

At the initial migration-validation stage, prompts and FAQ data were retained.
The final Go prompt and follow-up retrieval include the subsequently verified
[sharing-response correction](../sharing-response-incident.md); FAQ data and the
compatible persisted index remain unchanged. Narrow identity, asynchronous takeover,
buffer-reset and failed-media retry corrections are listed in the validation reports.
The original fixture-only validation did not send real Telegram messages or paid
provider requests. The later explicitly requested local stack startup and two
bounded candidate calls are documented separately in the incident and local-stack
reports. Remote deployment remains a separate action.
