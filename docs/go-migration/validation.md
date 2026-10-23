# Go verification

Go 1.27.3 checks use isolated unit tests, mock stores, in-process HTTP fakes and
frozen JSON contracts. Manual and opt-in PostgreSQL, sibling process and Docker
application/runtime integration harnesses were removed.

```sh
test -z "$(gofmt -l cmd internal)"
go mod tidy -diff
go mod verify
go vet ./...
go test -vet=off -count=1 ./...
go test -vet=off -race -count=1 ./...
CGO_ENABLED=0 go build -trimpath -o /tmp/supportbot ./cmd/supportbot
docker build -t supportbot:local .
```

Provider wire/configuration/formatting/router/scorer and FAQ unit contracts
remain. Production DB, migration and health behavior remain in application code.
The optional `cmd/verify cache` measures build/cache layers without starting an
application runtime. See [operations.md](operations.md) for current commands.
Historical migration measurements remain provenance, not current runtime results.
