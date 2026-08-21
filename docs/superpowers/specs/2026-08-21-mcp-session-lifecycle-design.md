# MCP Session Lifecycle Design

**Date:** 2026-08-21  
**Status:** Ready for implementation  
**Repositories:** `mitetenov/mcp-remnawave`, `mitetenov/SupportAiBot`

## Incident

After restarting only `support-bot`, the support chat received:

```text
[ОШИБКА БОТА]
Бот запущен БЕЗ инструментов Remnawave: MCP-сервер держит занятую сессию
(http://mcp-remnawave:3100). Перезапустите контейнер mcp-remnawave вместе с ботом.
no MCP tools exposed
```

The bot remained alive, but the model had no Remnawave tools and therefore could
not inspect subscriptions, nodes, or HWID devices.

## Root Cause

`mcp-remnawave` 3.2.0 creates one `StreamableHTTPServerTransport` and one
`McpServer` for the lifetime of the Node process. The first bot process
initializes that transport successfully. A replacement bot process sends a new
`initialize` request without the old `Mcp-Session-Id`; the already initialized
transport rejects it with `Server already initialized` and does not reveal the
old session identifier.

The HTTP router also rejects `DELETE /` with 405, while the sessionful
Streamable HTTP transport expects clients to terminate an owned session with an
HTTP DELETE carrying `Mcp-Session-Id`. The Python client instead sends
`notifications/cancelled`, which cancels an in-flight request and is not a
session shutdown operation.

The deploy workflow currently hides the defect by recreating every Compose
service, including PostgreSQL and `mcp-remnawave`, whenever only the bot is
deployed. Manual restarts and crash recovery still reproduce the incident.

## Goals

- A fresh bot process can initialize while the MCP server remains running.
- `mcp-remnawave` routes each valid session ID to its own transport and server.
- One session can be deleted without affecting other sessions.
- Graceful bot shutdown sends HTTP DELETE for its current session.
- Restarting only `support-bot` no longer produces `no MCP tools exposed`.
- Restarting only `mcp-remnawave` remains recoverable by the bot's existing
  one-time reinitialization and tool-call replay.
- Deploying the bot no longer recreates PostgreSQL or MCP as a workaround.
- Admin alerts describe the actual initialization/tool-list failure and do not
  assert that a session is occupied when that has not been established.

## Non-goals

- Persisting MCP sessions across a restart of the MCP server.
- Adding Redis or database-backed session state.
- Adding multiple MCP replicas or load-balancer session affinity.
- Changing the Remnawave tool allowlist, support-mode redaction, or readonly
  behavior.
- Adding a long-lived GET/SSE stream; the support bot uses request-scoped POST
  responses.
- Replacing the custom Python MCP client with another SDK.

## Architecture

### `mcp-remnawave`

Introduce an `HttpSessionManager` between the HTTP router and the MCP SDK. An
`initialize` request without `Mcp-Session-Id` creates a new
`StreamableHTTPServerTransport`, creates a fresh configured `McpServer`, connects
them, and stores the transport after the SDK assigns a session ID. Requests with
a known session ID are routed to that transport. Unknown IDs return 404. DELETE
is forwarded to the transport; its close callback removes the map entry.

Each session gets a fresh `McpServer`, as required by the SDK's one-transport
connection model. All sessions use the same immutable application configuration
but independent `RemnawaveClient` instances. Process shutdown closes every
active transport before exiting.

### `SupportAiBot`

Keep the existing initialize, tool cache, expiry detection, and one-time replay.
Record the negotiated protocol version and include it on later requests. On
shutdown, send DELETE with `Mcp-Session-Id` instead of
`notifications/cancelled`. Treat 404 (already gone) and 405 (old server without
termination support) as successful cleanup so shutdown stays backward
compatible during rollout.

### Rollout

Release and deploy `mcp-remnawave:v3.2.1` first. Then merge the bot client and
Compose pin update. Once v3.2.1 is pinned, remove the deploy workflow's global
`--force-recreate`; Compose should replace only services whose image or
configuration changed.

## Acceptance Criteria

1. Two independent initialize handshakes against one MCP process both return
   200 and distinct `Mcp-Session-Id` values.
2. Both sessions can call `tools/list` successfully.
3. Deleting session A makes A return 404, leaves session B usable, and does not
   prevent creation of session C.
4. `HttpMcpClient.close()` sends one DELETE with both `Mcp-Session-Id` and the
   negotiated `Mcp-Protocol-Version` and sends no cancellation notification.
5. A 404 or 405 DELETE response does not turn graceful shutdown into an error.
6. `docker compose restart support-bot` leaves `mcp-remnawave` running and the
   new bot exposes the expected Remnawave tools.
7. `docker compose restart mcp-remnawave` followed by a tool call causes one
   client reinitialization and a successful replay.
8. The support group receives no occupied-session alert during either restart
   scenario.

## Protocol Reference

- MCP Streamable HTTP session management and HTTP DELETE termination:
  <https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/transports.mdx>
- SDK 1.29.0 reference implementation: `simpleStreamableHttp` creates one
  transport/server pair per session, indexes transports by `Mcp-Session-Id`,
  and forwards DELETE to the selected transport.
