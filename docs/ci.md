# Native Go CI and image publication

Go 1.27.3 is pinned in `go.mod` and the Dockerfile. CI uses `go-version-file`,
Ubuntu 26.04 and exact released Action commit SHAs. Weekly Dependabot covers Go,
Docker and Actions, groups minor/patch updates and leaves major updates separate.

| Check | Coverage |
| --- | --- |
| `test` | Module tidy/verify, formatting, vet, golangci-lint v2.15.0, isolated unit/mock tests, race detector, static production build |
| `image-check (amd64)` | Linux AMD64 Docker build without registry credentials or runtime execution |

The required `test` name remains stable. PR runs cancel obsolete versions;
publication runs are not cancelled. Go modules/build caches are separated by
platform and job with manifest/source keys and fallback prefixes. Explicit vet
runs once; unit and race tests use `-vet=off -count=1` and execute on cache hits.

Tests requiring real PostgreSQL/pgvector, separate MCP binaries, child processes
and Docker runtime smoke were removed by request. In-process HTTP fakes, mock
stores and frozen JSON behavior fixtures remain. Production DB/migration code,
production health commands and the build-cache diagnostic remain available.
DB/check wrappers and manual Docker runtime/smoke harnesses were also removed.

On master, `build` waits for `test`. A dedicated serialized job covers registry
version allocation, overwrite refusal and Docker Hub publication. `VERSION` and
existing tags determine the next free version. The linux/amd64 image retains
release-version, commit-SHA and `latest` tags. PRs never publish. BuildKit caches
preserve dependency/runtime layers and compiler mounts; build records retain one day.

Deployment is a separate manual workflow. Its Docker tag is validated against
the tag grammar before SSH execution, and transferred through `BOT_TAG` instead
of interpolation into the remote shell. Validation never starts deployment.
