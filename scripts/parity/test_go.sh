#!/usr/bin/env bash
# Compatibility entrypoint; database initialization and checks are native Go.
set -euo pipefail
exec go run ./cmd/check "$@"
