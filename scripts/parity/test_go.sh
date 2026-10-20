#!/usr/bin/env bash
# Use a disposable database: suites create/drop isolated schemas in it.
set -euo pipefail
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to an isolated PostgreSQL/pgvector test database}"
# Initialize once before Go starts package tests concurrently. Installing the
# extension from a temporary schema can otherwise race with another package or
# remove the shared vector type when that temporary schema is dropped.
psql --quiet "$TEST_DATABASE_URL" --set=ON_ERROR_STOP=1 \
  --command='CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public'
exec go test -race -count=1 "$@" ./...
