# Native Go CI and image publication

Pull requests run two independent required checks in parallel:

| Check | Coverage |
| --- | --- |
| `test` | Module tidiness/integrity, formatting, vet, repository policy, native race tests and real PostgreSQL/pgvector |
| `image-check (amd64)` | Build and load Linux AMD64 image, execute read-only/non-root smoke with local fake APIs, durable history/restart/shutdown, static ELF, CA/DNS/TLS and assets |

The check names remain stable for branch protection. The image check does not
wait for the Go suite; merging still requires both to pass. Obsolete PR runs
are canceled when a newer commit arrives. Master runs are not canceled by the
PR concurrency group. Jobs have bounded 20-minute timeouts.

The recommended toolchain is pinned to Go 1.27.3 in `go.mod`, matching the image's
Dockerfile default; CI uses that directive via setup-go. The minimum module
language version stays 1.26. Runners are pinned to Ubuntu 24.04 so automatic OS
rollouts do not invalidate caches or silently change the verification environment.

Go module/compilation caches are keyed by OS, architecture, exact toolchain,
job, both module manifests and all source/embedded fixture inputs. Prefix
fallbacks reuse the preceding source snapshot, including after dependency changes.
A final same-toolchain fallback seeds new jobs from the default branch cache;
PR image jobs can reuse compilation populated by master verification/publication.
Each job saves its own updated compilation cache, so the race suite, native
container tools and release helper do not compete to save one immutable key.
Module integrity is verified and race tests always execute with `-count=1`;
restored caches never skip tests. Explicit vet runs once for all packages, so
the subsequent race command uses `-vet=off`. The image job compiles its verifier
once and invokes it for both smoke and runtime validation.

On master, `build` waits for the full Go test gate. A dedicated concurrency
group serializes the complete registry-version selection, overwrite check and
push job. VERSION and existing Docker Hub tags select the next free release;
partial registry listings fail publication. Linux AMD64 images are tagged with
the release version, commit SHA and `latest`. BuildKit's separate GHA cache is
retained in `mode=max` for dependency and runtime layers. PRs never publish.

The deployment workflow remains manual. CI publication and deployed state are
separate; neither a merge nor a registry tag confirms a running server update.
