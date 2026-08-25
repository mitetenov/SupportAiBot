# MCP SDK v2 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести два MCP-сервера и MCP-клиент supportBot на последние стабильные SDK v2, сохранив tool contracts, security boundaries и независимый rollout.

**Architecture:** `mcp-remnawave` получает split TypeScript SDK v2 и явную dual-era HTTP-маршрутизацию: modern `2026-07-28` обслуживается stateless handler, legacy-клиенты продолжают работать через sessionful transport. `bedolaga-mcp` переходит на Python `MCPServer` v2, который обслуживает обе eras одним ASGI app. `supportBot` заменяет hand-written JSON-RPC/SSE client на официальный Python `Client` v2, сохраняя текущий интерфейс `McpRouter` и независимое восстановление каждого backend.

**Tech Stack:** TypeScript 6, Node.js 22, `@modelcontextprotocol/server@2.0.0`, `@modelcontextprotocol/node@2.0.0`, Zod 4; Python 3.14, `mcp==2.0.0`, uv, Starlette/Uvicorn, pytest/unittest; Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-25-mcp-sdk-v2-migration-design.md`

## Global Constraints

- Во всех трёх репозиториях ветка называется `feat/mcp-sdk-v2` и создаётся только от свежего `origin/master` или `origin/main` после `git fetch --prune origin`.
- Использовать stable SDK `2.0.0`; alpha, beta, rc и плавающий `latest` запрещены.
- Не переносить в новые ветки локальные ahead-коммиты или изменения из других feature branches.
- Не менять имена, описания, аргументы и бизнес-результаты существующих MCP tools/resources/prompts, кроме wire-полей, которые SDK v2 добавляет сам.
- Не менять owner-based allowlists, collision fail-closed, Remnawave readonly gate и identity pinning в `supportBot`.
- Modern `2026-07-28` работает без session ID; legacy handshake до `2025-11-25` остаётся sessionful и поддерживает независимые `GET`/`POST`/`DELETE` операции.
- MCP endpoints остаются внутренними; Host/Origin validation должна разрешать loopback и Docker service names, но отклонять произвольный Host.
- Не добавлять Redis, БД, sticky-session infrastructure, OAuth или новые protocol extensions.
- План не содержит тестового исходного кода. Реализующий агент добавляет/обновляет покрытие по явным спискам «Покрытие агентом» и не снижает существующие quality gates.
- Не логировать session IDs, API keys, Authorization headers, tool payloads с персональными данными или сырые upstream response bodies.
- Коммиты делать отдельно в каждом репозитории и не смешивать dependency migration, transport migration и deployment pin updates в один commit.

---

## File Structure Map

### `/Users/mikhail/mcp-remnawave`

- `package.json`, `package-lock.json` — split SDK v2 dependencies и release version `3.3.0`.
- `src/server.ts` — единый `McpServer` factory для обеих protocol eras.
- `src/http-session-manager.ts` — только legacy sessionful transport, ownership server/transport и cleanup.
- `src/http-handler.ts` — `/health`, Host/Origin checks, body conversion, `isLegacyRequest` routing.
- `src/http-index.ts` — composition root для modern handler, legacy manager и graceful shutdown.
- `src/index.ts` — dual-era stdio через `serveStdio`.
- `src/support-filter.ts` — фильтрация только актуальных `registerTool`, `registerResource`, `registerPrompt`.
- `src/tools/helpers.ts` — tool result/error formatting; его контракт результата остаётся прежним.
- `src/tools/api-tokens.ts`, `bandwidth.ts`, `connections.ts`, `external-squads.ts`, `hosts.ts`, `hwid.ts`, `inbounds.ts`, `infra-billing.ts`, `keygen.ts`, `metadata.ts`, `node-integrations.ts`, `node-plugins.ts`, `nodes.ts`, `settings.ts`, `snippets.ts`, `squads.ts`, `subscription-page-configs.ts`, `subscriptions.ts`, `system.ts`, `users.ts` — native v2 tool registration и Zod v4 schemas.
- `src/tools/index.ts`, `src/prompts/index.ts`, `src/resources/index.ts` — v2 types и registration APIs.
- `README.md` — v2 dependencies, modern/legacy behavior, version `3.3.0`, rollout/rollback.
- Existing Vitest files under `tests/` — agent-owned regression coverage; этот план не вставляет их source.

### `/Users/mikhail/bedolaga-mcp`

- `requirements.txt` — `mcp==2.0.0`.
- `bedolaga_mcp/server.py` — `MCPServer` v2 factory, public `version=` и transport-free construction.
- `bedolaga_mcp/http.py` — transport configuration в `streamable_http_app()` и сохранение app lifespan.
- `bedolaga_mcp/stdio.py` — v2 stdio lifecycle без изменения shared client cleanup.
- `bedolaga_mcp/tools/__init__.py` — новый `func_metadata` import и явно unstructured JSON-text tools.
- `bedolaga_mcp/__init__.py` — release version `1.2.0` и актуальные docstrings.
- `README.md` — modern/legacy semantics, SDK/version table, curl examples и rollout.
- Existing unittest files under `tests/` — agent-owned regression coverage; тестовый source здесь не приводится.

### `/Users/mikhail/supportBot`

- `pyproject.toml`, `uv.lock` — Python MCP SDK v2 dependency и reproducible lock.
- `app/llm/mcp_client.py` — официальный v2 Client adapter, pagination, result rendering, reconnect и lifecycle.
- `app/llm/__init__.py` — убрать exports hand-written wire helpers.
- `app/main.py` — создавать независимые SDK-backed clients без передачи общего `httpx.AsyncClient` в MCP transport.
- `docker-compose.yml`, `.env.example` — defaults `MCP_TAG=v3.3.0`, `BEDOLAGA_MCP_TAG=1.2.0`.
- `README.md`, `CI-CD.md` — compatibility matrix, deployment order и rollback.
- `tests/test_mcp_client.py`, `tests/test_main.py`, `tests/test_regressions.py` — agent-owned coverage новой lifecycle модели; тестовый source здесь не приводится.

---

### Task 1: Create clean branches from the latest remote bases

**Files:**
- No tracked files.

**Interfaces:**
- Consumes: remote refs `origin/master` in supportBot and `origin/main` in both MCP repositories.
- Produces: three isolated branches named `feat/mcp-sdk-v2`, each with a merge base equal to its current remote base.

- [ ] **Step 1: Load the required worktree workflow**

Use `superpowers:using-git-worktrees` before creating any implementation worktree. Keep one worktree per repository; do not develop in the currently checked-out feature branch of `mcp-remnawave` or the locally-ahead `bedolaga-mcp/main`.

- [ ] **Step 2: Refresh and branch supportBot**

Run `git fetch --prune origin` in `/Users/mikhail/supportBot`, then ask the required worktree workflow to create branch `feat/mcp-sdk-v2` from start point `origin/master`. In the resulting isolated worktree, run:

```bash
git rev-parse --abbrev-ref HEAD
git merge-base --is-ancestor origin/master HEAD
git rev-list --left-right --count HEAD...origin/master
```

Expected immediately after branch creation: branch name `feat/mcp-sdk-v2` and `0 0` from `rev-list`.

- [ ] **Step 3: Refresh and branch mcp-remnawave**

Run `git fetch --prune origin` in `/Users/mikhail/mcp-remnawave`, then ask the required worktree workflow to create branch `feat/mcp-sdk-v2` from start point `origin/main`. In the resulting isolated worktree, run:

```bash
git rev-parse --abbrev-ref HEAD
git merge-base --is-ancestor origin/main HEAD
git rev-list --left-right --count HEAD...origin/main
```

Expected immediately after branch creation: `0 0`; commit `fedfc51b` from the current local feature branch is absent unless it has meanwhile been merged into `origin/main`.

- [ ] **Step 4: Refresh and branch bedolaga-mcp**

Run `git fetch --prune origin` in `/Users/mikhail/bedolaga-mcp`, then ask the required worktree workflow to create branch `feat/mcp-sdk-v2` from start point `origin/main`. In the resulting isolated worktree, run:

```bash
git rev-parse --abbrev-ref HEAD
git merge-base --is-ancestor origin/main HEAD
git rev-list --left-right --count HEAD...origin/main
```

Expected immediately after branch creation: `0 0`; local-only commit `cc415e1` is absent unless it has meanwhile been merged into `origin/main`.

- [ ] **Step 5: Record the actual bases in the implementation handoff**

Run `git rev-parse HEAD` once in every new worktree and record the three SHA values in the PR descriptions. If remote moved after this plan was authored, the fetched SHA is authoritative.

**Покрытие агентом:** no runtime coverage in this task; agent must prove all three branches start at the fetched remote tips and contain no unrelated local commits.

---

### Task 2: Move mcp-remnawave to split TypeScript SDK v2 and native registrations

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `src/server.ts`
- Modify: `src/support-filter.ts`
- Modify: `src/tools/index.ts`
- Modify: `src/tools/api-tokens.ts`
- Modify: `src/tools/bandwidth.ts`
- Modify: `src/tools/connections.ts`
- Modify: `src/tools/external-squads.ts`
- Modify: `src/tools/hosts.ts`
- Modify: `src/tools/hwid.ts`
- Modify: `src/tools/inbounds.ts`
- Modify: `src/tools/infra-billing.ts`
- Modify: `src/tools/keygen.ts`
- Modify: `src/tools/metadata.ts`
- Modify: `src/tools/node-integrations.ts`
- Modify: `src/tools/node-plugins.ts`
- Modify: `src/tools/nodes.ts`
- Modify: `src/tools/settings.ts`
- Modify: `src/tools/snippets.ts`
- Modify: `src/tools/squads.ts`
- Modify: `src/tools/subscription-page-configs.ts`
- Modify: `src/tools/subscriptions.ts`
- Modify: `src/tools/system.ts`
- Modify: `src/tools/users.ts`
- Modify: `src/prompts/index.ts`
- Modify: `src/resources/index.ts`
- Agent coverage updates: `tests/support-filter.test.ts`, `tests/support.test.ts` and registration/schema assertions in the existing Vitest suite.

**Interfaces:**
- Consumes: `Config`, `RemnawaveClient`, `SUPPORT_TOOLS`, `SUPPORT_RESOURCES`, `SUPPORT_PROMPTS`, and existing callback result shapes.
- Produces: `createServer(config: Config): McpServer` using only v2 classes; exactly 179 tools, 4 resources and 5 prompts in full mode; the existing support profile in restricted mode.

- [ ] **Step 1: Add v2 packages before removing v1**

Run:

```bash
npm install @modelcontextprotocol/server@2.0.0 @modelcontextprotocol/node@2.0.0
npm uninstall @modelcontextprotocol/sdk
```

Keep `zod` on its existing `^4.4.3` range; it is already above the v2 minimum `^4.2.0`. `package-lock.json` must resolve both MCP packages to `2.0.0` and contain no `node_modules/@modelcontextprotocol/sdk` entry.

- [ ] **Step 2: Rewrite SDK and Zod imports**

Use these public imports consistently:

```ts
import { McpServer, ResourceTemplate } from '@modelcontextprotocol/server';
import * as z from 'zod/v4';
```

Files that only need a type use `import type { McpServer } ...`. Do not import v2 internals or `@modelcontextprotocol/core-internal`.

- [ ] **Step 3: Convert every tool registration to the v2 config shape**

For every former variadic call, use the native API and a wrapped Zod object:

```ts
server.registerTool(
    'users_get_by_telegram_id',
    {
        description: 'Get a user by Telegram ID',
        inputSchema: z.object({ telegramId: z.number() }),
    },
    async ({ telegramId }) => toolResult(await client.getUserByTelegramId(telegramId)),
);
```

For a no-argument tool, still pass `inputSchema: z.object({})`; otherwise v2 treats the callback's first argument as request context rather than an argument object. Preserve every current description, `.optional()`, `.default()`, `.describe()`, enum and callback body.

- [ ] **Step 4: Convert prompts and resources**

Use `registerPrompt(name, { description, argsSchema: z.object(...) }, callback)` for all five prompts. Use `registerResource(name, uriOrTemplate, metadata, callback)` for all four resources; keep an explicit metadata object even when empty. Preserve URI templates and MIME types exactly.

- [ ] **Step 5: Tighten support-mode interception to the v2 surface**

In `src/support-filter.ts`, keep only:

```ts
const GATED_METHODS: Record<string, ReadonlySet<string>> = {
    registerTool: SUPPORT_TOOLS,
    registerResource: SUPPORT_RESOURCES,
    registerPrompt: SUPPORT_PROMPTS,
};
```

The proxy must still bind non-gated methods to the real `McpServer`, drop disallowed registrations, and never turn `.server` into an allowlist bypass. Update comments that claim the removed short aliases still exist.

- [ ] **Step 6: Confirm the migration is complete mechanically**

Run:

```bash
rg -n '@modelcontextprotocol/sdk|server\.(tool|resource|prompt)\(' src tests package.json package-lock.json
npm run build
npm test
```

Expected: no old SDK import or short registration call; build and all tests pass.

- [ ] **Step 7: Commit the dependency and registry migration**

```bash
git add package.json package-lock.json src tests
git commit -m "chore(mcp): migrate registrations to SDK v2"
```

**Покрытие агентом:** exact full/support tool counts; exact support allowlists; no duplicate names; all input schemas remain root objects and retain required/default/description metadata; callbacks receive `{}` for no-argument tools rather than context; resource URI templates and prompt arguments remain callable; `toolResult` and `toolError` preserve text content and `isError`; no v1 class crosses into a v2 server.

---

### Task 3: Add dual-era HTTP and stdio serving to mcp-remnawave

**Files:**
- Modify: `src/http-session-manager.ts`
- Modify: `src/http-handler.ts`
- Modify: `src/http-index.ts`
- Modify: `src/index.ts`
- Agent coverage updates: `tests/http-session-manager.test.ts`, `tests/http-handler.test.ts` and a modern v2 protocol integration scenario in the Vitest suite.

**Interfaces:**
- Consumes: `createServer(config)`, `NodeStreamableHTTPServerTransport`, `createMcpHandler`, `isLegacyRequest`, `isInitializeRequest`, `toNodeHandler`, `toWebRequest`.
- Produces: one endpoint `/` that serves modern stateless requests and legacy independent sessions; `GET /health`; `closeAll(): Promise<void>` for legacy sessions; `modern.close(): Promise<void>` for in-flight modern exchanges.

- [ ] **Step 1: Migrate the legacy session manager to v2 types**

Replace `StreamableHTTPServerTransport` with
`NodeStreamableHTTPServerTransport` from `@modelcontextprotocol/node`, and import
`McpServer` plus `isInitializeRequest` from `@modelcontextprotocol/server`.

Store one record per session:

```ts
type LegacySession = {
    transport: NodeStreamableHTTPServerTransport;
    server: McpServer;
};
```

On `onsessioninitialized`, index the record. On transport close, remove it and close the associated server. `closeAll()` clears the map first, then closes every unique transport and server with `Promise.allSettled`; throw one `AggregateError` containing all shutdown failures.

- [ ] **Step 2: Preserve complete legacy routing semantics**

The legacy manager must implement:

- POST without session + valid `initialize` → create a fresh server/transport;
- POST/GET/DELETE with a known session → route to that transport;
- supplied unknown session → HTTP 404 `Session not found`;
- no session on a non-initialize operation → HTTP 400;
- DELETE closes only its own session;
- no log line includes the session ID.

- [ ] **Step 3: Build a strict modern handler**

In `src/http-index.ts` construct:

```ts
const modern = createMcpHandler(() => createServer(config), {
    legacy: 'reject',
    responseMode: 'json',
});
const modernNode = toNodeHandler(modern);
```

`responseMode: 'json'` is valid because current tools do not emit mid-call notifications. The legacy manager remains the only owner of handshake traffic.

- [ ] **Step 4: Route protocol eras in the HTTP handler**

Keep `GET /health` outside MCP routing. Accept only `/` for MCP. For each MCP request:

1. convert the Node request once with `await toWebRequest(req)`;
2. validate Host and Origin against `localhost`, `127.0.0.1`, `::1` and `mcp-remnawave` using public server validation helpers;
3. call `await isLegacyRequest(request)` before consuming JSON;
4. parse POST JSON from the still-readable web request;
5. call the legacy manager or `modernNode(req, res, parsedBody)`.

Malformed JSON and unsupported content type must return SDK-compatible 4xx responses, not a generic 500. Arbitrary Host such as `attacker.example` must fail before MCP dispatch. Requests without `Origin` remain valid for service-to-service traffic.

- [ ] **Step 5: Make shutdown cover both eras**

After the Node HTTP listener stops accepting requests, close modern in-flight handlers and legacy sessions, even if one close fails. Set a non-zero process exit code on aggregate failure. Repeated SIGINT/SIGTERM must still execute shutdown once.

- [ ] **Step 6: Use dual-era stdio serving**

Replace direct `StdioServerTransport` construction with:

```ts
import { serveStdio } from '@modelcontextprotocol/server/stdio';

const config = loadConfig();
await serveStdio(() => createServer(config));
```

This gives stdio the same modern probe and legacy fallback as HTTP without duplicating registrations.

- [ ] **Step 7: Verify transport behavior**

Run:

```bash
npm run build
npm test -- tests/http-handler.test.ts tests/http-session-manager.test.ts
npm test
```

Expected: both eras discover and call the same tools; legacy session isolation and modern no-session behavior pass; shutdown leaves no active handler.

- [ ] **Step 8: Commit dual-era serving**

```bash
git add src/http-session-manager.ts src/http-handler.ts src/http-index.ts src/index.ts tests
git commit -m "feat(mcp): serve modern and legacy protocol eras"
```

**Покрытие агентом:** modern `server/discover`, modern `tools/list` and one `tools/call` with no `Mcp-Session-Id`; two simultaneous legacy clients; legacy GET stream routing; independent DELETE and replacement session; missing versus unknown session status; Host/Origin allow/deny matrix; invalid JSON/content type; modern and legacy graceful shutdown; server restart causes legacy 404 but does not affect a fresh modern call.

---

### Task 4: Release and document mcp-remnawave 3.3.0

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `src/server.ts`
- Modify: `README.md`

**Interfaces:**
- Consumes: completed v2 registry and dual-era transports.
- Produces: consistent server/package version `3.3.0` and operator documentation for image tag `v3.3.0`.

- [ ] **Step 1: Bump all live version sources**

Set `package.json`, root package entries in `package-lock.json`, and `McpServer` serverInfo version in `src/server.ts` to `3.3.0`. Do not rewrite historical plan/spec documents that intentionally describe 3.2.1.

- [ ] **Step 2: Update both README language sections**

Document:

- TypeScript SDK packages at `2.0.0`;
- modern `2026-07-28` requests are stateless and have no session ID;
- legacy clients retain sessionful POST/GET/DELETE behavior;
- endpoint and health URL are unchanged;
- old `supportBot` remains compatible during server-first rollout;
- rollback to `v3.2.1` is allowed because the new supportBot client can use legacy fallback.

- [ ] **Step 3: Verify release consistency**

Run:

```bash
node -e "const p=require('./package.json'); const l=require('./package-lock.json'); if (p.version !== '3.3.0' || l.version !== p.version || l.packages[''].version !== p.version) process.exit(1)"
rg -n '3\.2\.1|@modelcontextprotocol/sdk' package.json package-lock.json src README.md
npm run build
npm test
```

Only historical upgrade text may still mention `3.2.1`; no active version label or dependency may do so.

- [ ] **Step 4: Commit release metadata**

```bash
git add package.json package-lock.json src/server.ts README.md
git commit -m "docs(mcp): prepare remnawave MCP 3.3.0"
```

**Покрытие агентом:** initialize/server discovery reports `3.3.0` in both eras; package and lock versions agree; Docker build still starts `dist/http-index.js`; README commands use `/` and the correct image tag.

---

### Task 5: Migrate bedolaga-mcp to Python MCP SDK v2

**Files:**
- Modify: `requirements.txt`
- Modify: `bedolaga_mcp/server.py`
- Modify: `bedolaga_mcp/http.py`
- Modify: `bedolaga_mcp/stdio.py`
- Modify: `bedolaga_mcp/tools/__init__.py`
- Modify: `bedolaga_mcp/__init__.py`
- Agent coverage updates: `tests/test_tool_registry.py`, `tests/test_transport_security.py` and transport lifecycle coverage in the existing unittest suite.

**Interfaces:**
- Consumes: `register_tools(server)`, `_transport_security()`, `close_client()`, existing `/health` response and eight tool handlers.
- Produces: `create_server() -> MCPServer`, `create_app(transport_host: str = "0.0.0.0")`, one dual-era Starlette app at `/`, shared stdio factory, version `1.2.0`.

- [ ] **Step 1: Pin the stable Python SDK**

Change only the MCP line to:

```text
mcp==2.0.0
```

Keep `uvicorn==0.52.4` and `httpx==0.28.1` pinned unless dependency resolution proves an actual conflict. Do not add `mcp-types` directly; `mcp` pins its matching version.

- [ ] **Step 2: Replace FastMCP with MCPServer**

Use:

```python
from mcp.server import MCPServer

server = MCPServer(
    name=SERVER_NAME,
    version=__version__,
)
```

Remove transport options from `create_server()` and delete the private
`server._mcp_server.version` mutation. The factory owns identity and tools only.

- [ ] **Step 3: Move transport settings into create_app**

Make the app factory accept the configured bind host and, after registering the existing health custom route, return:

```python
return server.streamable_http_app(
    host=transport_host,
    streamable_http_path="/",
    json_response=True,
    stateless_http=False,
    transport_security=_transport_security(),
)
```

Pass `config.mcp_http_host` from `run()` into `create_app()` so SDK validation and Uvicorn bind configuration cannot diverge. The app's own lifespan must remain attached so the v2 session manager starts and stops. Keep `_BedolagaHTTPServer` responsible for closing the shared upstream `httpx` client after Uvicorn finishes.

- [ ] **Step 4: Update tool metadata internals deliberately**

Move `func_metadata` to:

```python
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
```

Type `register_tools` against `MCPServer`. Pass `structured_output=False` when adding each existing handler so the published contract remains JSON in text content, as documented, instead of silently adding a new output schema during an SDK-only migration.

- [ ] **Step 5: Preserve stdio cleanup**

Continue using `await server.run_stdio_async()` inside the existing signal-aware event loop. Rename FastMCP references in comments/docstrings, but retain lazy shared client creation, normal EOF cleanup, and signal cleanup.

- [ ] **Step 6: Bump the server release version**

Set `bedolaga_mcp.__version__` to `1.2.0`. Update version assertions owned by the agent as part of coverage; do not alter the eight tool names.

- [ ] **Step 7: Verify Python server migration**

Run:

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -c "from bedolaga_mcp.server import create_server; s=create_server(); assert s.version == '1.2.0'"
rg -n 'FastMCP|mcp\.server\.fastmcp|_mcp_server|mcp==1\.' bedolaga_mcp requirements.txt
```

Expected: no v1 import/private attribute, all tests pass, server reports `1.2.0`.

- [ ] **Step 8: Commit the Python migration**

```bash
git add requirements.txt bedolaga_mcp tests
git commit -m "chore(mcp): migrate bedolaga server to SDK v2"
```

**Покрытие агентом:** modern official Client negotiates `2026-07-28`, lists exactly eight tools and calls one; legacy initialize returns a session and version `1.2.0`; legacy DELETE affects one session only; Host allows `bedolaga-mcp:3100` and loopback but rejects attacker Host; `/health` exposes no config; stdio and HTTP expose identical names/schemas; dict results remain one JSON text block; shared upstream client closes once on HTTP shutdown, stdio EOF and signal.

---

### Task 6: Document and prepare bedolaga-mcp 1.2.0

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: dual-era `MCPServer` from Task 5.
- Produces: operator and client documentation for image tag `1.2.0`.

- [ ] **Step 1: Correct transport terminology**

Replace statements that call Streamable HTTP universally sessionful with a two-era explanation: modern `2026-07-28` is stateless/sessionless; legacy handshake clients receive `Mcp-Session-Id` and can terminate that session with DELETE.

- [ ] **Step 2: Update connection examples**

Keep Claude/Hermes/Cursor URL examples unchanged. Label raw initialize curl as a legacy compatibility check. Add an official SDK v2 client example that negotiates automatically; do not require users to construct `_meta` manually.

- [ ] **Step 3: Update versions and rollback text**

Set the compatibility table to `bedolaga-mcp 1.2.0`, Python MCP SDK `2.0.0`, protocol `2026-07-28` plus legacy through `2025-11-25`. State that rollback image is `1.1.0` and that supportBot v2 will auto-fallback.

- [ ] **Step 4: Verify docs and container**

Run:

```bash
rg -n '1\.1\.0|FastMCP|транспорт использует stateful-сессии|Sessionful MCP' README.md
docker build -t bedolaga-mcp:1.2.0 .
python3 -m unittest discover -s tests -v
```

Historical release notes may mention `1.1.0`; active setup/version tables must not.

- [ ] **Step 5: Commit release docs**

```bash
git add README.md
git commit -m "docs(mcp): prepare bedolaga MCP 1.2.0"
```

**Покрытие агентом:** README URLs and ports match runtime; health and legacy curl commands are executable; modern example observes protocol `2026-07-28`; no statement promises session IDs to modern clients.

---

### Task 7: Replace supportBot's hand-written MCP wire client with Python SDK v2

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `app/llm/mcp_client.py`
- Modify: `app/llm/__init__.py`
- Agent coverage updates: `tests/test_mcp_client.py`, `tests/test_regressions.py`.

**Interfaces:**
- Consumes: `mcp.client.Client`, `mcp.types.Implementation`, `CallToolResult`, current `AdminNotifier`, `McpClientInterface`, `McpTool` and router string-result contract.
- Produces: `HttpMcpClient.init() -> bool`, synchronous cached `list_tools() -> list[McpTool]`, `call_tool(...) -> str`, idempotent `close() -> None`, and `protocol_version: str | None` without manual JSON-RPC code.

- [ ] **Step 1: Add the SDK dependency and regenerate the lock**

Add to project dependencies:

```toml
"mcp>=2.0.0,<3.0.0",
```

Run:

```bash
uv lock --upgrade-package mcp
uv sync --all-extras
```

Confirm `uv.lock` resolves `mcp` and matching `mcp-types` to `2.0.0`. Keep existing direct `httpx` because the rest of supportBot uses it; MCP v2's `httpx2` remains SDK-owned.

- [ ] **Step 2: Delete the hand-written protocol layer**

Remove constants and helpers for JSON-RPC IDs, fixed protocol version, headers, SSE extraction, explicit initialize, initialized notification, manual tools/list, manual tools/call and DELETE. Remove `McpSessionExpired`, `looks_like_expired_session`, `extract_json_from_sse`, `uuid` and direct MCP use of `httpx`.

Keep the public `McpTool`, `AdminNotifier`, `McpClientInterface` and `HttpMcpClient` names so the router and composition root do not need a new abstraction.

- [ ] **Step 3: Introduce an SDK client factory seam**

Add a constructor-only factory used to create a fresh SDK Client for initial connect and every reconnect:

```python
type McpSdkClientFactory = Callable[[str], Client]
```

The default factory builds `Client(url, mode="auto", client_info=Implementation(name="vpn-support-bot", version="2.0.1"))`. Tests may inject a fake context-managed Client, but production passes only URL and notifier. Never reuse a `Client` after its async context exits.

- [ ] **Step 4: Own the async context with AsyncExitStack**

`init()` creates a new `AsyncExitStack`, enters the SDK Client, stores its negotiated `protocol_version`, walks every `list_tools(cursor=...)` page, converts definitions to `McpTool`, then marks the wrapper initialized. On any failure it closes the partial stack, clears all state, logs the server label and sends the existing admin notification.

Do not require a session ID: successful modern initialization has none by design.

- [ ] **Step 5: Normalize tool results for the LLM**

Implement one focused `render_tool_result(result: CallToolResult) -> str` helper with the exact precedence from the design spec. Tool-level `is_error` returns safe JSON and notifies admins but does not reconnect. Structured content wins over duplicate text. One text block is returned verbatim. Multiple/non-text blocks use wire aliases and JSON mode. Use `ensure_ascii=False` and never `str(result)`.

- [ ] **Step 6: Reconnect only a broken transport/session**

Retain a per-instance lock and generation counter. If SDK raises `MCPError` for `Session not found`, connection closed, or a terminated transport, the first caller closes the dead stack, creates a fresh Client, reloads tools, increments generation and retries the call once. Other concurrent callers observe the new generation and reuse it.

Do not reconnect for `CallToolResult.is_error`, unknown tool, invalid arguments or a normal Bedolaga `ok:false` envelope. A failed retry returns safe `{"error": ...}` and alerts admins once with the owning server label.

- [ ] **Step 7: Make close idempotent**

`close()` atomically detaches and closes the active `AsyncExitStack`, then clears client, tools, protocol version and initialized state. SDK context exit handles modern transport shutdown and legacy DELETE. A second close is a no-op. No shared LLM/Telegram `httpx.AsyncClient` is owned or closed by this wrapper.

- [ ] **Step 8: Remove obsolete package exports**

In `app/llm/__init__.py`, stop importing/exporting `McpException` and `extract_json_from_sse`. Keep `HttpMcpClient`, `McpClientInterface` and `McpTool` exported.

- [ ] **Step 9: Verify the adapter**

Run:

```bash
uv run pytest tests/test_mcp_client.py tests/test_regressions.py -v
uv run ruff check app tests
uv run mypy app
uv run pytest
```

Expected: all gates pass at the existing coverage threshold; no hand-written wire helper remains.

- [ ] **Step 10: Commit the client migration**

```bash
git add pyproject.toml uv.lock app/llm/mcp_client.py app/llm/__init__.py tests/test_mcp_client.py tests/test_regressions.py
git commit -m "refactor(mcp): use the official SDK v2 client"
```

**Покрытие агентом:** auto-negotiated modern connection; fallback to a v1 legacy server; multi-page tools/list; mapping optional descriptions and input schemas; structured, single-text, multi-block and `is_error` results; Bedolaga `ok:false` is not transport failure; session-not-found reconnect; concurrent failures create one replacement Client; changed tool set refreshes cache and is logged without payloads; failed reconnect; partial-init cleanup; legacy DELETE on close; modern close without DELETE assumption; repeated close; no server failure disables the other client.

---

### Task 8: Wire SDK-backed clients into supportBot startup and shutdown

**Files:**
- Modify: `app/main.py`
- Agent coverage updates: `tests/test_main.py` and relevant startup/shutdown scenarios in `tests/test_regressions.py`.

**Interfaces:**
- Consumes: `HttpMcpClient(server_name, base_url, admin_notifier)` from Task 7.
- Produces: independently initialized Remnawave and optional Bedolaga clients, the unchanged `McpRouter`, and orderly close after message/ticket drains.

- [ ] **Step 1: Stop passing the shared httpx client into MCP**

Construct both MCP wrappers with only `server_name`, `base_url` and `admin_notifier`. Update comments from “one session per server” to “one independent SDK client/tool cache/recovery lock per server”; modern clients are sessionless.

- [ ] **Step 2: Preserve independent degraded startup**

Initialize each client separately. A failed Remnawave connection must not suppress healthy Bedolaga tools and vice versa. Keep collision reporting and the per-owner “zero allowed tools” alert unchanged.

- [ ] **Step 3: Preserve shutdown ordering**

Keep message and ticket drains before MCP close. Close every wrapper before the shared application `http_client`. If one MCP close raises despite its internal tolerance, continue closing the second client and the remaining app resources, then report the aggregate shutdown problem.

- [ ] **Step 4: Verify composition behavior**

Run:

```bash
uv run pytest tests/test_main.py tests/test_mcp_router.py tests/test_regressions.py -v
uv run pytest
```

- [ ] **Step 5: Commit composition changes**

```bash
git add app/main.py tests/test_main.py tests/test_regressions.py
git commit -m "refactor(mcp): manage v2 clients independently"
```

**Покрытие агентом:** both clients healthy; each single-server failure permutation; Bedolaga disabled; one backend returns zero allowed tools; collision alert; exact router owner binding; shutdown after drains; one close failure does not leak the other client or shared app resources.

---

### Task 9: Pin v2-compatible server releases and document supportBot rollout

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CI-CD.md`

**Interfaces:**
- Consumes: released images `mitetenov/remnawave-mcp:v3.3.0` and `mitetenov/bedolaga-mcp:1.2.0`.
- Produces: supportBot defaults that launch both v2 servers and operator instructions for independent upgrade/rollback.

- [ ] **Step 1: Verify immutable images exist before changing defaults**

Run only after the two server branches have merged and their release workflows finish:

```bash
docker buildx imagetools inspect mitetenov/remnawave-mcp:v3.3.0
docker buildx imagetools inspect mitetenov/bedolaga-mcp:1.2.0
```

Do not point production defaults at tags that have not been published.

- [ ] **Step 2: Update Compose and environment defaults**

Set:

```yaml
image: mitetenov/remnawave-mcp:${MCP_TAG:-v3.3.0}
image: mitetenov/bedolaga-mcp:${BEDOLAGA_MCP_TAG:-1.2.0}
```

And in `.env.example`:

```dotenv
MCP_TAG=v3.3.0
BEDOLAGA_MCP_TAG=1.2.0
```

Do not change service names, ports, health dependencies or internal-only network exposure.

- [ ] **Step 3: Update the compatibility matrix**

README must state:

- supportBot uses Python MCP SDK `2.0.0` in `auto` mode;
- Remnawave image default is `v3.3.0`;
- Bedolaga image default is `1.2.0`;
- preferred protocol is `2026-07-28`;
- fallback supports old servers through `2025-11-25`.

Keep the exact eight Bedolaga allowlisted tool names and current Remnawave profile unchanged.

- [ ] **Step 4: Document server-first deployment**

Use this order in README and CI-CD:

```bash
cp .env .env.pre-mcp-sdk-v2
docker compose pull mcp-remnawave bedolaga-mcp
docker compose up -d --wait mcp-remnawave bedolaga-mcp
docker compose pull support-bot
docker compose up -d --wait --no-deps support-bot
```

Before the pull, operators set `MCP_TAG=v3.3.0`, `BEDOLAGA_MCP_TAG=1.2.0` and the immutable new `BOT_TAG` in `.env`. The backup contains secrets and must retain restrictive permissions; never attach it to a ticket or PR.

- [ ] **Step 5: Document independent rollback**

State three supported paths:

- bot-only failure: restore only previous `BOT_TAG`; keep v2 servers;
- one server failure: restore that server's previous tag (`v3.2.1` or `1.1.0`); v2 client auto-falls back;
- whole rollout failure: restore `.env.pre-mcp-sdk-v2` and recreate only the three affected services.

No database rollback or volume deletion is part of any path.

- [ ] **Step 6: Verify active references**

Run:

```bash
rg -n 'MCP_TAG|BEDOLAGA_MCP_TAG|3\.2\.1|1\.1\.0|2025-11-25|2026-07-28' docker-compose.yml .env.example README.md CI-CD.md
docker compose config
uv run pytest
```

Historical upgrade sections may retain old versions; defaults and compatibility tables must use the new releases.

- [ ] **Step 7: Commit deployment support**

```bash
git add docker-compose.yml .env.example README.md CI-CD.md
git commit -m "chore(mcp): pin SDK v2 compatible servers"
```

**Покрытие агентом:** Compose resolves exact default tags; both health dependencies remain; Bedolaga feature flag is independent from ticket integration; bot connects by Docker service names accepted by Host validation; documented rollback does not recreate Postgres or delete volumes.

---

### Task 10: Run the cross-repository compatibility gate and staged release

**Files:**
- No additional source files unless a discovered incompatibility requires a focused fix in the owning repository.

**Interfaces:**
- Consumes: mcp-remnawave `3.3.0`, bedolaga-mcp `1.2.0`, supportBot feature branch and previous server images.
- Produces: evidence for all four client/server combinations and a reversible production rollout.

- [ ] **Step 1: Run repository-local gates from clean worktrees**

In `mcp-remnawave`:

```bash
npm ci
npm run build
npm test
```

In `bedolaga-mcp`:

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
docker build -t bedolaga-mcp:1.2.0 .
```

In `supportBot`:

```bash
uv sync --all-extras
uv run ruff check app tests
uv run mypy app
uv run pytest
docker compose config
```

- [ ] **Step 2: Verify the compatibility matrix**

Exercise and record all combinations:

| Client | Server | Expected protocol | Expected behavior |
|---|---|---|---|
| old supportBot | mcp-remnawave 3.3.0 / bedolaga 1.2.0 | legacy ≤ `2025-11-25` | initialize, session ID, tools list/call, DELETE |
| new supportBot | old Remnawave 3.2.1 / Bedolaga 1.1.0 | legacy fallback | tools remain available and reconnect works |
| new supportBot | both new servers | `2026-07-28` | discover, no session ID, tools list/call |
| independent standard v2 client | each new server | `2026-07-28` | advertised names/schemas/results match contracts |

Use synthetic/non-production identities and read-only calls. For the only allowed Remnawave deletes, use a disposable fixture and prove idempotent replay separately; do not delete a real user's HWID.

- [ ] **Step 3: Verify failure isolation and recovery**

With the new stack running:

1. restart only `mcp-remnawave`; confirm Bedolaga tools remain available and the next Remnawave call succeeds;
2. restart only `bedolaga-mcp`; confirm Remnawave tools remain available and the next Bedolaga call succeeds;
3. restart only `support-bot`; confirm modern requests create no server session cleanup errors;
4. force a legacy client connection, restart its server, and confirm one reconnect creates a fresh session;
5. inspect logs for secrets, full session IDs and raw personal tool payloads; expected result is no matches.

- [ ] **Step 4: Release servers before supportBot**

Merge and publish `mcp-remnawave` first, then `bedolaga-mcp`. Verify immutable image manifests and health. Only then merge supportBot's Compose pin/client changes.

- [ ] **Step 5: Deploy and observe**

Follow Task 9's server-first commands. Record image digests, negotiated protocol per backend, tool counts after allowlists, health states and admin-alert absence. Observe at least one real support turn that uses each backend without recording personal tool results in the deployment log.

- [ ] **Step 6: Roll back only the failing component if a gate fails**

Use the exact rollback paths from Task 9. After rollback, repeat health, discovery and one safe tool call for both backends. Do not merge a workaround that pins `mode="legacy"` globally; that would conceal the failed v2 migration rather than fix it.

**Покрытие агентом:** all four compatibility combinations; modern/legacy negotiation; exact tool counts after router allowlists; server-only and bot-only restarts; concurrent calls during reconnect; graceful shutdown; image architecture manifests; no secret/session leakage; documented rollback verified without database mutation.

---

## Completion Criteria

- All three branches were created from fresh remote base refs and contain only scoped commits.
- `mcp-remnawave` has no `@modelcontextprotocol/sdk` dependency/import and ships `3.3.0` on TypeScript SDK v2.
- `bedolaga-mcp` has no FastMCP v1 import/private version mutation and ships `1.2.0` on Python SDK v2.
- `supportBot` has no hand-written MCP JSON-RPC/SSE/initialize implementation and negotiates through Python SDK v2.
- New client/new servers use `2026-07-28`; old/new mixed combinations use legacy fallback successfully.
- Tool names, schemas, allowlists, identity pinning, readonly policy and safe result envelopes remain intact.
- Local tests, lint, type checks, builds, Docker config/build and cross-repository smoke matrix pass without lowering gates.
- Server-first rollout and independent rollback are documented and exercised.
