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
```

Текущий suite содержит только изолированные unit/mock-тесты. Обёртка с реальным
PostgreSQL/pgvector, optional sibling MCP process acceptance и ручные Docker
smoke/runtime harnesses удалены вместе с тестовыми probe-утилитами.
Производственная команда `/app/supportbot healthcheck` сохранена.

```sh
docker build -t supportbot-go:complete .
go run ./cmd/verify cache --image supportbot-go:complete --results /tmp/cache-results
```

Cache records build-layer behavior without starting the application, database or
provider fixtures. It can create a BuildKit builder and stopped image-extraction
containers; it is a build diagnostic, not an application runtime test.

The former external-API/disposable-DB benchmark CLI was removed. Pure scorer and
mocked unit fixtures remain under internal/eval. The release helper
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
