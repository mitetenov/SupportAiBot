#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
common_git_dir=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)
primary=$(dirname "$common_git_dir")
export SUPPORTBOT_ENV_FILE="${SUPPORTBOT_ENV_FILE:-$primary/.env}"
export REMNAWAVE_MCP_SOURCE="${REMNAWAVE_MCP_SOURCE:-$(dirname "$primary")/mcp-remnawave}"
export BEDOLAGA_MCP_SOURCE="${BEDOLAGA_MCP_SOURCE:-$(dirname "$primary")/bedolaga-mcp}"
if [[ ! -f "$SUPPORTBOT_ENV_FILE" ]]; then
  printf 'Missing local env file; set SUPPORTBOT_ENV_FILE.\n' >&2
  exit 1
fi
if [[ $# -eq 0 ]]; then set -- up -d --build; fi
exec docker compose --project-name supportbot-local-go \
  --env-file "$SUPPORTBOT_ENV_FILE" -f "$root/docker-compose.local.yml" "$@"
