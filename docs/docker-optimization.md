> Runtime/smoke test harnesses were removed; historical measurements below are
> archival. Only the build-cache diagnostic remains executable.

# Docker image and cache optimization

Measured locally on 2026-10-20. The previous Go image `supportbot-go:parity` was **33,264,068 bytes**; `supportbot-go:optimized` is **18,446,327 bytes**: **14,817,741 bytes saved (44.55%)**. These are Docker-reported local image sizes, not compressed registry download sizes. Runtime filesystem layers decreased from five to four.

The Go binary remains statically linked, stripped and built with `-trimpath`. The runtime now uses `scratch` with the existing CA bundle, numeric UID/GID `100:101`, `/app` working directory/HOME, FAQ JSON/images and executable native healthcheck. It needs no shell, libc, temporary writes or Python. Named-zone data was absent from the previous Alpine runtime and is not used by the current UTC business logic. Logs and diagnostics remain in the binary.

## Layer layout

- `go.mod`/`go.sum` precede source compilation. `go mod download` writes into a regular dependency layer, so imported external caches retain the modules even on a fresh runner. BuildKit cache mounts are [not exported to the GitHub Actions cache by default](https://docs.docker.com/build/ci/github-actions/cache/#cache-mounts).
- Go source is bound read-only for compilation; FAQ changes do not invalidate compilation. The compiler cache mount is separated by Go version, target OS, architecture and variant. The Actions cache uses the image-specific `supportbot-go` scope.
- CA/identity files, FAQ and binary use separate `COPY --link` layers. The binary is copied as an explicit `/out/app/supportbot` tree with epoch timestamps on both file and parent directory. This avoids a wall-clock timestamp on a synthetic `/app` directory changing the binary layer after FAQ-only edits.
- The build context is an allowlist of module manifests, `cmd`, `internal` (including embedded prompts) and FAQ assets; tests, fixtures, host tools, documentation and all `.env` files are excluded. Warm incremental transfer bytes are not presented as total context size.

## Measured cache behavior

Each probe used a unique temporary source/FAQ/module-manifest change. The dependency probe also changed `godotenv` from v1.5.1 to v1.5.0 only in its disposable context.

| Rebuild | Dependency download | Go compile | Runtime layer result |
| --- | --- | --- | --- |
| Unchanged | Cached | Cached | FAQ and binary cached |
| Source-only | Cached | Executed | CA/FAQ reused; comment-only probe produced identical binary layer |
| FAQ-only | Cached | Cached | FAQ changed; binary layer digest identical |
| Dependency change | Executed | Executed | CA/FAQ reused; binary changed |
| Fresh builder importing external cache, changed source | Cached | Executed | No Go module redownloads despite empty cache mounts |

For FAQ-only changes BuildKit reports the linked binary COPY as `DONE` while assembling the image, but its uncompressed layer digest is identical: `sha256:a61cb71211b06409334cb9fc228f845bceafb477d3036aaba2eccaf3503fc1dc`. This is layer reuse evidence rather than merely identical executable bytes. Full step statuses, layer digests and timings are in [the evidence JSON](docker-optimization-results.json).

The fresh-builder check uses a portable local external cache, not a remote Actions run. Docker Desktop emitted nonfatal `service.Write unavailable` diagnostics during cache export; export exited successfully, and the new isolated builder imported the cache and compiled changed source without module downloads. The GitHub workflow and registry publication were not run.

## Runtime verification and reproduction

Native Linux/arm64 startup passed with a read-only root filesystem, isolated real PostgreSQL/pgvector and local Telegram/LLM/MCP mocks: FAQ indexing, customer answer, commands/help, persisted two-message history, native healthcheck and graceful exit. A separate probe passed static ELF checks, DNS, local HTTPS, CA parsing (121 roots), FAQ JSON/PNG reads, UID/GID and working directory. The default CA file is byte-identical to the baseline (181,724 bytes). No production API was called. Linux/amd64 final image cross-build also passed; its runtime was not executed.

```sh
docker buildx build --load -t supportbot-go:optimized .
docker buildx build --load --platform linux/amd64 -t supportbot-go:optimized-amd64 .
go run ./cmd/verify cache --image supportbot-go:optimized
```

Host prerequisites are Go and Docker with Buildx. TLS certificates and the probe
are generated with Go; no additional scripting runtime or OpenSSL is required.
The smoke command creates and removes its own isolated PostgreSQL container,
network, bot container and local fake APIs. The cache command creates temporary
contexts/builders/images, validates layer reuse and cleans them up. It retains
the selected image and writes per-build logs and JSON measurements to its results
directory (`--results /tmp/cache-results`). The optional baseline image checks
byte-for-byte public CA equivalence. No production configuration is read.
