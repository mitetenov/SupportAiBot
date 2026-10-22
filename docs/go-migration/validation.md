# Go verification

Run formatting, module checks, vet and the complete race suite with an explicit
disposable PostgreSQL/pgvector database:

```sh
test -z "$(gofmt -l cmd internal)"
go mod tidy
git diff --exit-code -- go.mod go.sum
go mod verify
go vet ./...
TEST_DATABASE_URL='postgres://test:test@127.0.0.1:5432/test?sslmode=disable' \
  go run ./cmd/check -json
```

Set `TEST_REMNAWAVE_MCP_BIN` / `TEST_BEDOLAGA_MCP_BIN` to exercise actual sibling
MCP binaries against local backend fixtures. Those optional cross-repository
checks require their own executables; protocol/router fixtures always run.

Go tests retain the original JSON/SQL behavioral contracts for provider wire
formats, configuration, formatting, persisted data and customer/ticket flows.
The native evaluator also checks 146 captured scorer contracts with exact
verdicts/violations, tool identity, request observation privacy, report overwrite
protection, independent retrieval modes and comparison with production pgvector.
FAQ tests retain confirmed payment, sharing, installation, app/platform and
instruction-order policies and check every referenced image.

Docker smoke/runtime commands in [operations.md](operations.md) validate actual
containers with isolated local APIs. CI runs those on Linux AMD64 after building
the image. CI does not use production secrets or paid provider calls. Historical
JSON measurements from the initial migration remain provenance, not new results.
