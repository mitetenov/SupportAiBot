# Running the local native Go stack

The `supportbot-local-go` Compose project was started locally on 2026-10-20 and is intentionally left running. It contains the native Go support bot, two native Go MCP servers and PostgreSQL17 with pgvector. All four containers are healthy; each application has zero restarts.

| Service | Image | Local health endpoint |
| --- | --- | --- |
| support-bot | `supportbot-go:optimized` | http://127.0.0.1:8080/health |
| mcp-remnawave | `supportbot-local/remnawave-mcp:go` | http://127.0.0.1:13100/health |
| bedolaga-mcp | `supportbot-local/bedolaga-mcp:go` | http://127.0.0.1:13101/health |
| pgvector | `pgvector/pgvector:pg17` | Internal Docker network only |

The bot uses the existing test bot [@peipivoaitestbot](https://t.me/peipivoaitestbot). Its Telegram webhook is absent; polling is running with no observed409 conflicts. No artificial Telegram messages or paid conversational LLM test requests were sent. Normal startup registered bot commands and used the configured embedding provider to persist55 FAQ rows with55 vectors. Bedolaga's webhook/ticket poller remains disabled by the existing configuration; its MCP is enabled.

## Commands

```sh
cd /path/to/SupportAiBot
./scripts/local-stack.sh up -d --build
./scripts/local-stack.sh ps
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:13100/health
curl --fail http://127.0.0.1:13101/health
./scripts/local-stack.sh stop
```

`stop` stops the services and preserves their data. `up -d` starts them again. The persistent database volume is `supportbot-local-go_pgvector-data`; do not remove this volume to retain local history and FAQ vectors.

The launcher locates the primary checkout through Git and reads its existing `.env` directly. It defaults the sibling source directories to `mcp-remnawave` and `bedolaga-mcp` beside the primary checkout. Override `SUPPORTBOT_ENV_FILE`, `REMNAWAVE_MCP_SOURCE` or `BEDOLAGA_MCP_SOURCE` if those paths change. Secrets are neither copied into Git nor embedded into the images.

The local Compose file overrides the bot's MCP URLs to the local service DNS names `http://mcp-remnawave:3100/` and `http://bedolaga-mcp:3100/`, and database host to `pgvector`. Ports bind only to loopback. INFO logging overrides the source `.env` TRACE setting for this local session. The bot runs with a read-only root filesystem. Existing provider credentials and backend URLs remain configured; those external backend services are not hosted by this Compose project.

## Verification

Native executable exports identify Go1.27.3 for the bot/Remnawave and Go1.27.2 for Bedolaga, with ELF executables and no Python runtime. Bot startup logs show successful connections to both local MCPs: Remnawave17 discovered tools and Bedolaga8. The native healthchecks and all three HTTP health endpoints pass. PostgreSQL contains55 persisted FAQ vectors. Exact image IDs and safe startup evidence are in [local-go-stack-results.json](local-go-stack-results.json).

These checks establish a working local stack and MCP discovery. They do not claim a synthetic conversation or a paid LLM request was tested. No remote deployment or remote service configuration change was performed.
