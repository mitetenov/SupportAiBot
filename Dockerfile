# syntax=docker/dockerfile:1.7

# =============================================================================
# Stage 1: Build virtual environment with uv
# =============================================================================
FROM ghcr.io/astral-sh/uv:latest AS uv-bin

FROM python:3.12-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv-bin /uv /uvx /bin/

# Install dependencies from the lock file, so an image built today and one built
# in six months contain the same versions. --no-install-project keeps this layer
# dependent on pyproject.toml/uv.lock alone: application code is copied later and
# runs from the working directory, so editing it does not re-resolve anything.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.12-slim AS runtime

# Set runtime environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create non-root user and group
RUN groupadd -r bot && useradd -r -g bot -d /app -s /sbin/nologin bot

WORKDIR /app

# Copy virtualenv and application code
COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot app/ app/
COPY --chown=bot:bot faq/ faq/

USER bot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["python3", "-m", "app.main"]
