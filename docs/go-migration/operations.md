# Native Go operation and compatibility

The application and developer tools use Go. The Docker image contains a static
`/app/supportbot` binary, CA certificates and FAQ assets, runs as UID/GID 100:101
and supports a read-only filesystem. Configuration names, PostgreSQL tables,
FAQ indexing fingerprints, conversation identities and MCP contracts are retained.

## Build and verification

```sh
go build -o supportbot ./cmd/supportbot
./supportbot
./supportbot healthcheck
go vet ./...
go test -race ./...
TEST_DATABASE_URL='postgres://test:test@127.0.0.1:5432/test?sslmode=disable' \
  go run ./cmd/check -json
```

`cmd/check` requires an explicitly supplied disposable PostgreSQL/pgvector
database, initializes the shared vector extension once, then runs all packages
with the race detector and no test-result cache. Tests create isolated schemas.
The compatibility shell launcher `scripts/parity/test_go.sh` delegates to it.
Optional `TEST_REMNAWAVE_MCP_BIN` and `TEST_BEDOLAGA_MCP_BIN` enable actual sibling
binary acceptance against local upstream fixtures. No production provider is used.

```sh
docker build -t supportbot-go:complete .
go run ./cmd/verify smoke --image supportbot-go:complete
go run ./cmd/verify runtime --image supportbot-go:complete
go run ./cmd/verify cache --image supportbot-go:complete --results /tmp/cache-results
```

Smoke creates and removes its own isolated PostgreSQL, network, fake APIs and
bot container. It checks health, commands, help, answers, MCP discovery, durable
history, restart, read-only/non-root operation and graceful shutdown. Runtime
checks static ELF, certificates, container DNS, local TLS, identity and assets;
`--baseline-image IMAGE` additionally compares CA bundles. Cache checks unchanged,
source-only, FAQ-only, dependency and fresh-builder imported-cache behavior.
All use Go and Docker without loading application `.env`.

Benchmark commands and external-API opt-in are documented in
[the evaluator guide](../../benchmarks/PROMPT_QUALITY.md). The release helper
`go run ./cmd/nextversion mitetenov/supportbot` reads the major version from
`VERSION` and assigns the next unused minor from Docker Hub. Incomplete tag
listings fail publication; an independent manifest check prevents overwrites.

## Configuration and data

The bot reads `.env` and then gives environment variables precedence.
`BOT_LOG_LEVEL` accepts TRACE, INFO and ERROR; `LOG_LEVEL` is a legacy fallback.
INFO records operational metadata; opt-in TRACE includes sanitized conversational
payloads capped at 20,000 characters. Binary attachments and credentials are omitted.
`TELEGRAM_API_BASE_URL` supports self-hosted Bot API and local fixtures.

Startup is idempotent and retains the existing signed 64-bit identities,
nullable ticket fields, timestamps, token counts, history and watermarks. Old
naive timestamps are UTC. Compatible FAQ embeddings and fingerprints are kept;
legacy provider strings in persisted fingerprints intentionally remain stable.
A replacement FAQ index is committed only after all new embeddings are ready.
If replacement fails, retained text search stays available and startup retries.
Run one active bot per Telegram token and shared state, preserving existing
in-process locking and polling semantics.

## Deployment and rollback

Record the deployed image digest/configuration and back up PostgreSQL first.
Publish through the tested release workflow, stop the previous bot before
starting the new image, and retain the volume and independent MCP versions.
Verify real personal/cabinet conversations, attachments, operator handover and
provider access separately from local/CI fixtures. Rollback stops the new bot and
restores the recorded earlier image/configuration with the same volume. Restore
matching embedding configuration when changing model/dimension. Historical
migration measurements in this directory describe their recorded commits;
immutable baseline JSON/SQL is still checked by native Go tests. Retired source
and fixture-export scripts remain available in Git history at `45fb11a`.
