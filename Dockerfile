# syntax=docker/dockerfile:1.7
ARG GO_VERSION=1.27.3
FROM --platform=$BUILDPLATFORM golang:${GO_VERSION}-alpine AS base

FROM base AS builder
WORKDIR /src
COPY go.mod go.sum ./
# Keep modules in an ordinary layer: external GHA caches do not export cache mounts.
# This layer is shared by target architectures and survives source/FAQ changes.
RUN go mod download
ARG GO_VERSION
ARG TARGETOS=linux
ARG TARGETARCH
ARG TARGETVARIANT
RUN --mount=type=bind,source=cmd,target=/src/cmd,readonly \
    --mount=type=bind,source=internal,target=/src/internal,readonly \
    --mount=type=cache,id=supportbot-go-${GO_VERSION}-${TARGETOS}-${TARGETARCH}-${TARGETVARIANT},target=/root/.cache/go-build \
    mkdir -p /out/app \
    && CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -mod=readonly -buildvcs=false -trimpath -ldflags='-s -w' \
    -o /out/app/supportbot ./cmd/supportbot \
    && chmod 0555 /out/app/supportbot \
    && touch -t 197001010000 /out/app /out/app/supportbot

FROM base AS runtime-files
# Preserve the original bot UID/GID and HOME; the process needs no shell or libc.
RUN mkdir -p /rootfs/etc/ssl/certs /rootfs/app \
    && cp /etc/ssl/certs/ca-certificates.crt /rootfs/etc/ssl/certs/ \
    && printf 'bot:x:100:101:SupportBot:/app:/sbin/nologin\n' > /rootfs/etc/passwd \
    && printf 'bot:x:101:\n' > /rootfs/etc/group

FROM scratch AS runtime
COPY --link --from=runtime-files /rootfs/ /
WORKDIR /app
# Independent linked layers reuse FAQ/certificates even when the binary changes.
# Root-owned assets are readable, but not writable, by the bot.
COPY --link faq/ /app/faq/
# Include the explicit parent directory to avoid wall-clock COPY directory metadata.
COPY --link --from=builder /out/ /
ENV HOME=/app
USER 100:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s CMD ["/app/supportbot", "healthcheck"]
ENTRYPOINT ["/app/supportbot"]
