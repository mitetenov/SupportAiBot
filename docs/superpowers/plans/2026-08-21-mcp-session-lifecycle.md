# MCP Session Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `support-bot` and `mcp-remnawave` independently restartable without losing the Remnawave tools or requiring a coupled container restart.

**Architecture:** Replace the singleton HTTP transport in `mcp-remnawave` with a session manager that creates and indexes one MCP server/transport pair per `Mcp-Session-Id`, routes POST and DELETE by that ID, and closes all sessions on process shutdown. Update the Python client to negotiate the current Streamable HTTP protocol, send the negotiated protocol header, and terminate its owned session with HTTP DELETE; then remove the Compose-wide force-recreate workaround.

**Tech Stack:** Node.js 22, TypeScript 6, `@modelcontextprotocol/sdk` 1.29.0, Vitest 4, Python 3.14, httpx 0.28+, pytest 8, Docker Compose, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-21-mcp-session-lifecycle-design.md`

## Global Constraints

- Work in two sibling checkouts: `mcp-remnawave` and `SupportAiBot`; do not copy server source into the bot repository.
- Implement and release `mcp-remnawave:v3.2.1` before changing the production `SupportAiBot` pin to `v3.2.1`.
- Keep `@modelcontextprotocol/sdk` at `1.29.0`; this fix does not require an SDK upgrade.
- Keep Node.js at `>=22.0.0` and Python at `>=3.14`.
- Preserve `GET /health` as a liveness endpoint returning `200 {"status":"ok"}`.
- Preserve POST `/` as the bot's request path and add only DELETE `/`; do not add a standalone GET/SSE stream.
- Preserve support mode's 16-tool allowlist, credential redaction, and the Python router's readonly filtering.
- Treat session IDs as bearer-like values: do not include their full values in application logs or admin messages.
- A missing session ID on a non-initialize request is HTTP 400; an unknown supplied session ID is HTTP 404.
- DELETE cleanup is best effort in the Python client: 200/202/204, 404, and 405 are non-fatal during shutdown.
- Do not add Redis, PostgreSQL tables, a named volume, or another session persistence mechanism.
- Do not lower existing coverage, lint, formatting, or type-check gates.

---

## File Structure

### `mcp-remnawave`

- Create `src/http-session-manager.ts`: owns the in-memory `session ID -> transport` map, creates one configured `McpServer` per initialize request, routes session requests, and closes active transports.
- Create `tests/http-session-manager.test.ts`: real HTTP/SDK regression tests for two simultaneous sessions, independent DELETE, unknown sessions, and reinitialization.
- Modify `src/http-handler.ts`: continue serving `/health`, allow POST and DELETE on `/`, parse a body only for POST, and delegate protocol decisions to the session manager.
- Modify `tests/http-handler.test.ts`: cover DELETE delegation and retain GET/invalid-method boundaries.
- Modify `src/http-index.ts`: construct the session manager rather than a singleton transport/server and close all sessions during shutdown.
- Modify `package.json`, `package-lock.json`, `src/server.ts`, and `README.md`: publish the fix consistently as 3.2.1 and document multi-session HTTP plus DELETE.

### `SupportAiBot`

- Modify `app/llm/mcp_client.py`: store the negotiated protocol version, send `Mcp-Protocol-Version`, stop retrying an unowned initialize blindly, and terminate with DELETE.
- Modify `tests/test_mcp_client.py`: prove negotiated headers, DELETE cleanup, 404/405 compatibility, and a single initialize attempt.
- Modify `tests/test_regressions.py`: keep production wiring assertions aligned with the negotiated protocol header and DELETE shutdown.
- Modify `app/main.py`: distinguish initialization failure from an empty allowed-tool set and remove the stale occupied-session assertion.
- Modify `tests/test_main.py` and `tests/test_regressions.py`: cover both startup alert branches without triggering an alert on the healthy composition-root fixture.
- Modify `docker-compose.yml`, `.env.example`, `README.md`, and `CI-CD.md`: pin `mcp-remnawave:v3.2.1` and document independent restart behavior.
- Modify `.github/workflows/deploy.yml`: deploy only `support-bot`; do not force-recreate MCP or PostgreSQL.

---

### Task 1: Add the failing multi-session HTTP regression tests to `mcp-remnawave`

**Files:**
- Create: `[mcp-remnawave]/tests/http-session-manager.test.ts`

**Interfaces:**
- Consumes: `createHttpHandler(target: McpRequestTarget)`, `McpServer`, and the MCP 2025-11-25 initialize envelope.
- Produces: executable acceptance tests for `HttpSessionManager.handleRequest(req, res, body)` and `HttpSessionManager.closeAll()` that Task 2 must satisfy.

- [ ] **Step 1: Create the integration-test harness and first failing two-session test**

Create `tests/http-session-manager.test.ts` with a real ephemeral Node HTTP server. Use the SDK rather than a mocked transport so the test catches the exact singleton defect:

```ts
import http, { type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { createHttpHandler } from '../src/http-handler.js';
import { HttpSessionManager } from '../src/http-session-manager.js';

const PROTOCOL_VERSION = '2025-11-25';

type Running = {
    server: Server;
    sessions: HttpSessionManager;
    url: string;
};

const running: Running[] = [];

function testMcpServer(): McpServer {
    const server = new McpServer({ name: 'session-test', version: '1.0.0' });
    server.tool('ping', 'Test tool', {}, async () => ({
        content: [{ type: 'text', text: 'pong' }],
    }));
    return server;
}

async function start(): Promise<Running> {
    const sessions = new HttpSessionManager(testMcpServer);
    const server = http.createServer(createHttpHandler(sessions));
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address() as AddressInfo;
    const instance = { server, sessions, url: `http://127.0.0.1:${address.port}/` };
    running.push(instance);
    return instance;
}

async function jsonRpc(response: Response): Promise<Record<string, unknown>> {
    const text = await response.text();
    const data = text
        .split('\n')
        .find((line) => line.startsWith('data: '))
        ?.slice('data: '.length);
    return JSON.parse(data ?? text) as Record<string, unknown>;
}

async function initialize(url: string, name: string): Promise<string> {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json, text/event-stream',
        },
        body: JSON.stringify({
            jsonrpc: '2.0',
            id: 1,
            method: 'initialize',
            params: {
                protocolVersion: PROTOCOL_VERSION,
                capabilities: {},
                clientInfo: { name, version: '1.0.0' },
            },
        }),
    });
    expect(response.status).toBe(200);
    await jsonRpc(response);
    const sessionId = response.headers.get('mcp-session-id');
    expect(sessionId).toBeTruthy();
    return sessionId!;
}

async function post(url: string, sessionId: string, message: object): Promise<Response> {
    return fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json, text/event-stream',
            'Mcp-Session-Id': sessionId,
            'Mcp-Protocol-Version': PROTOCOL_VERSION,
        },
        body: JSON.stringify(message),
    });
}

async function markInitialized(url: string, sessionId: string): Promise<void> {
    const response = await post(url, sessionId, {
        jsonrpc: '2.0',
        method: 'notifications/initialized',
        params: {},
    });
    expect(response.status).toBe(202);
}

afterEach(async () => {
    for (const instance of running.splice(0)) {
        await instance.sessions.closeAll();
        await new Promise<void>((resolve, reject) =>
            instance.server.close((error) => (error ? reject(error) : resolve())),
        );
    }
});

describe('HttpSessionManager', () => {
    it('allows two clients to initialize and list tools independently', async () => {
        const { url } = await start();

        const sessionA = await initialize(url, 'client-a');
        const sessionB = await initialize(url, 'client-b');
        expect(sessionA).not.toBe(sessionB);

        await markInitialized(url, sessionA);
        await markInitialized(url, sessionB);

        const listA = await post(url, sessionA, {
            jsonrpc: '2.0', id: 2, method: 'tools/list', params: {},
        });
        const listB = await post(url, sessionB, {
            jsonrpc: '2.0', id: 2, method: 'tools/list', params: {},
        });

        expect(listA.status).toBe(200);
        expect(listB.status).toBe(200);
        expect(await jsonRpc(listA)).toMatchObject({
            result: { tools: [{ name: 'ping' }] },
        });
        expect(await jsonRpc(listB)).toMatchObject({
            result: { tools: [{ name: 'ping' }] },
        });
    });
});
```

- [ ] **Step 2: Add the failing DELETE-isolation test**

Append this test inside the same `describe` block:

```ts
it('deletes one session without breaking another or blocking a replacement', async () => {
    const { url } = await start();
    const sessionA = await initialize(url, 'client-a');
    const sessionB = await initialize(url, 'client-b');
    await markInitialized(url, sessionA);
    await markInitialized(url, sessionB);

    const deleted = await fetch(url, {
        method: 'DELETE',
        headers: {
            Accept: 'application/json, text/event-stream',
            'Mcp-Session-Id': sessionA,
            'Mcp-Protocol-Version': PROTOCOL_VERSION,
        },
    });
    expect(deleted.status).toBe(200);

    const dead = await post(url, sessionA, {
        jsonrpc: '2.0', id: 3, method: 'tools/list', params: {},
    });
    expect(dead.status).toBe(404);

    const live = await post(url, sessionB, {
        jsonrpc: '2.0', id: 3, method: 'tools/list', params: {},
    });
    expect(live.status).toBe(200);

    const sessionC = await initialize(url, 'client-c');
    expect(sessionC).not.toBe(sessionA);
    expect(sessionC).not.toBe(sessionB);
});
```

- [ ] **Step 3: Add the failing request-boundary test**

Append this test to pin 400 versus 404 behavior:

```ts
it('distinguishes a missing session from an unknown supplied session', async () => {
    const { url } = await start();

    const missing = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json, text/event-stream',
        },
        body: JSON.stringify({
            jsonrpc: '2.0', id: 1, method: 'tools/list', params: {},
        }),
    });
    expect(missing.status).toBe(400);

    const unknown = await post(url, 'unknown-session', {
        jsonrpc: '2.0', id: 2, method: 'tools/list', params: {},
    });
    expect(unknown.status).toBe(404);
});
```

- [ ] **Step 4: Run the new test and verify it fails for the intended reason**

Run:

```bash
npm test -- tests/http-session-manager.test.ts
```

Expected: FAIL at module resolution with `Cannot find module '../src/http-session-manager.js'`. Do not implement around a different failure.

- [ ] **Step 5: Commit the red tests**

```bash
git add tests/http-session-manager.test.ts
git commit -m "test(http): reproduce occupied MCP sessions"
```

---

### Task 2: Implement server-side session routing and DELETE

**Files:**
- Create: `[mcp-remnawave]/src/http-session-manager.ts`
- Modify: `[mcp-remnawave]/src/http-handler.ts:3-49`
- Modify: `[mcp-remnawave]/tests/http-handler.test.ts:6-69`
- Modify: `[mcp-remnawave]/src/http-index.ts:1-35`
- Test: `[mcp-remnawave]/tests/http-session-manager.test.ts`
- Test: `[mcp-remnawave]/tests/http-handler.test.ts`

**Interfaces:**
- Consumes: `serverFactory: () => McpServer`, SDK `isInitializeRequest(body)`, `StreamableHTTPServerTransport.handleRequest(...)`, and `transport.onclose`.
- Produces: `class HttpSessionManager implements McpRequestTarget`, `handleRequest(req, res, body): Promise<void>`, and `closeAll(): Promise<void>`.

- [ ] **Step 1: Implement `HttpSessionManager` minimally**

Create `src/http-session-manager.ts`:

```ts
import { randomUUID } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { isInitializeRequest } from '@modelcontextprotocol/sdk/types.js';
import type { McpRequestTarget } from './http-handler.js';

type ServerFactory = () => McpServer;

export class HttpSessionManager implements McpRequestTarget {
    private readonly transports = new Map<string, StreamableHTTPServerTransport>();

    constructor(private readonly serverFactory: ServerFactory) {}

    async handleRequest(
        req: IncomingMessage,
        res: ServerResponse,
        body?: unknown,
    ): Promise<void> {
        const sessionId = this.readSessionId(req);

        if (req.method === 'DELETE') {
            if (!sessionId) {
                this.reject(res, 400, 'Bad Request: No session ID provided');
                return;
            }
            const transport = this.transports.get(sessionId);
            if (!transport) {
                this.reject(res, 404, 'Session not found');
                return;
            }
            await transport.handleRequest(req, res);
            return;
        }

        if (req.method !== 'POST') {
            this.reject(res, 405, 'Method Not Allowed');
            return;
        }

        if (sessionId) {
            const transport = this.transports.get(sessionId);
            if (!transport) {
                this.reject(res, 404, 'Session not found');
                return;
            }
            await transport.handleRequest(req, res, body);
            return;
        }

        if (!isInitializeRequest(body)) {
            this.reject(res, 400, 'Bad Request: No valid session ID provided');
            return;
        }

        await this.initialize(req, res, body);
    }

    async closeAll(): Promise<void> {
        const active = [...new Set(this.transports.values())];
        this.transports.clear();
        const results = await Promise.allSettled(active.map((transport) => transport.close()));
        const failures = results
            .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
            .map((result) => result.reason);
        if (failures.length > 0) {
            throw new AggregateError(failures, 'Failed to close MCP HTTP sessions');
        }
    }

    private async initialize(
        req: IncomingMessage,
        res: ServerResponse,
        body: unknown,
    ): Promise<void> {
        let transport!: StreamableHTTPServerTransport;
        transport = new StreamableHTTPServerTransport({
            sessionIdGenerator: randomUUID,
            onsessioninitialized: (sessionId) => {
                this.transports.set(sessionId, transport);
            },
        });
        transport.onclose = () => {
            const sessionId = transport.sessionId;
            if (sessionId) {
                this.transports.delete(sessionId);
            }
        };

        const server = this.serverFactory();
        try {
            await server.connect(transport);
            await transport.handleRequest(req, res, body);
        } catch (error) {
            const sessionId = transport.sessionId;
            if (sessionId) {
                this.transports.delete(sessionId);
            }
            await transport.close().catch(() => undefined);
            throw error;
        }
    }

    private readSessionId(req: IncomingMessage): string | undefined {
        const value = req.headers['mcp-session-id'];
        return typeof value === 'string' && value.length > 0 ? value : undefined;
    }

    private reject(res: ServerResponse, status: number, message: string): void {
        res.writeHead(status, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
            jsonrpc: '2.0',
            error: { code: -32000, message },
            id: null,
        }));
    }
}
```

This stores the transport only from `onsessioninitialized`, not before the SDK has assigned the ID. It creates a new `McpServer` for each transport and never logs a session ID.

- [ ] **Step 2: Let the outer HTTP router forward DELETE**

Change `createHttpHandler` so `/health` remains special, POST and DELETE are allowed only on `/`, and a body is read only for POST:

```ts
export function createHttpHandler(target: McpRequestTarget) {
    return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
        if (req.method === 'GET' && req.url === '/health') {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ status: 'ok' }));
            return;
        }

        const isEndpoint = req.url === '/' || req.url === '';
        const isSupportedMethod = req.method === 'POST' || req.method === 'DELETE';
        if (!isEndpoint || !isSupportedMethod) {
            res.writeHead(405, { 'Content-Type': 'text/plain' });
            res.end('Method Not Allowed');
            return;
        }

        try {
            let parsedBody: unknown;
            if (req.method === 'POST') {
                const chunks: Buffer[] = [];
                for await (const chunk of req) {
                    chunks.push(chunk as Buffer);
                }
                const body = Buffer.concat(chunks).toString('utf-8');
                parsedBody = body ? JSON.parse(body) : undefined;
            }
            await target.handleRequest(req, res, parsedBody);
        } catch (error) {
            console.error('MCP request handler error:', error);
            if (!res.headersSent) {
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: 'Internal Server Error' }));
            }
        }
    };
}
```

Update the function comment from “POST-only” to “POST plus DELETE session termination; GET is reserved for `/health`.”

- [ ] **Step 3: Extend the focused router unit test**

Change the spy to record method and body:

```ts
function transportSpy() {
    const calls: Array<{ method?: string; body?: unknown }> = [];
    return {
        calls,
        transport: {
            async handleRequest(req: IncomingMessage, _res: ServerResponse, body?: unknown) {
                calls.push({ method: req.method, body });
            },
        },
    };
}
```

Change the POST assertion to:

```ts
expect(calls).toEqual([{
    method: 'POST',
    body: { jsonrpc: '2.0', method: 'tools/list', id: 1 },
}]);
```

Add:

```ts
it('hands DELETE on the MCP endpoint to the session target without parsing a body', async () => {
    const { transport, calls } = transportSpy();
    const { res } = response();

    await createHttpHandler(transport)(request('DELETE', '/'), res);

    expect(calls).toEqual([{ method: 'DELETE', body: undefined }]);
});
```

- [ ] **Step 4: Replace the singleton composition in `http-index.ts`**

Remove the top-level `McpServer` and `StreamableHTTPServerTransport`. Construct the manager with a factory so every initialize request gets a fresh configured server:

```ts
import http from 'node:http';
import { createHttpHandler } from './http-handler.js';
import { HttpSessionManager } from './http-session-manager.js';
import { loadConfig } from './config.js';
import { createServer } from './server.js';

const PORT = parseInt(process.env.MCP_HTTP_PORT ?? '3100', 10);
const HOST = process.env.MCP_HTTP_HOST ?? '0.0.0.0';

const config = loadConfig();
const sessions = new HttpSessionManager(() => createServer(config));
const httpServer = http.createServer(createHttpHandler(sessions));

httpServer.listen(PORT, HOST, () => {
    console.log(`MCP Remnawave HTTP server listening on http://${HOST}:${PORT}`);
});

let shuttingDown = false;
const shutdown = async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log('Shutting down MCP HTTP server...');
    await new Promise<void>((resolve, reject) =>
        httpServer.close((error) => (error ? reject(error) : resolve())),
    );
    await sessions.closeAll();
};

const handleSignal = () => {
    void shutdown().catch((error) => {
        console.error('MCP HTTP shutdown failed:', error);
        process.exitCode = 1;
    });
};

process.on('SIGTERM', handleSignal);
process.on('SIGINT', handleSignal);
```

Do not call `process.exit(0)` from the signal handler; once the listener and transports are closed, the event loop exits naturally and pending cleanup is not cut off.

- [ ] **Step 5: Run the focused tests**

```bash
npm test -- tests/http-handler.test.ts tests/http-session-manager.test.ts
```

Expected: both files PASS, including two concurrent initialized sessions and independent DELETE.

- [ ] **Step 6: Run all server gates**

```bash
npx tsc --noEmit
npm test
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the server behavior**

```bash
git add src/http-session-manager.ts src/http-handler.ts src/http-index.ts tests/http-handler.test.ts tests/http-session-manager.test.ts
git commit -m "fix(http): manage MCP sessions independently"
```

---

### Task 3: Version and document the fixed MCP server

**Files:**
- Modify: `[mcp-remnawave]/package.json:3`
- Modify: `[mcp-remnawave]/package-lock.json:2-10`
- Modify: `[mcp-remnawave]/src/server.ts:9-13`
- Modify: `[mcp-remnawave]/README.md:13,162-164,686-688`

**Interfaces:**
- Consumes: session behavior delivered by Task 2.
- Produces: immutable Docker/GitHub release tag `v3.2.1`, which the bot rollout in Task 6 pins.

- [ ] **Step 1: Set all runtime/package version sources to 3.2.1**

Change `package.json`:

```json
"version": "3.2.1"
```

Change the `McpServer` metadata in `src/server.ts`:

```ts
const server = new McpServer({
    name: 'remnawave-mcp',
    version: '3.2.1',
});
```

Change both English and Russian README version labels from `3.2.0` to `3.2.1`.

- [ ] **Step 2: Regenerate only package-lock metadata**

The checked-in lock currently reports the stale root version `1.3.1`. Normalize it from `package.json`:

```bash
npm install --package-lock-only
```

Verify:

```bash
node -e "const p=require('./package.json'); const l=require('./package-lock.json'); if (p.version !== '3.2.1' || l.version !== p.version || l.packages[''].version !== p.version) process.exit(1)"
```

Expected: exit 0.

- [ ] **Step 3: Document the HTTP lifecycle**

Replace the Docker transport paragraph in both README languages with text carrying these exact guarantees:

```markdown
The container runs the sessionful Streamable HTTP transport on port 3100.
`POST /` initializes a new independent session or routes a request by
`Mcp-Session-Id`; `DELETE /` terminates that session without affecting other
clients. `GET /health` answers `200 {"status":"ok"}` for container liveness.
```

Russian equivalent:

```markdown
В контейнере работает sessionful Streamable HTTP-транспорт на порту 3100.
`POST /` создаёт независимую сессию или маршрутизирует запрос по
`Mcp-Session-Id`; `DELETE /` завершает только указанную сессию, не затрагивая
других клиентов. `GET /health` возвращает `200 {"status":"ok"}` для проверки
живости контейнера.
```

- [ ] **Step 4: Re-run release gates**

```bash
npm ci
npx tsc --noEmit
npm test
npm run build
```

Expected: all commands exit 0 and `dist/http-index.js` is produced.

- [ ] **Step 5: Commit the release metadata**

```bash
git add package.json package-lock.json src/server.ts README.md
git commit -m "chore: prepare remnawave MCP 3.2.1"
```

- [ ] **Step 6: Open, review, and merge the `mcp-remnawave` PR before continuing**

Required merge order:

```text
mcp-remnawave tests green
→ merge to main
→ GitHub release v3.2.1 exists
→ Docker image mitetenov/remnawave-mcp:v3.2.1 exists
→ continue with the SupportAiBot pin
```

Do not merge a SupportAiBot change that defaults to an image tag which has not been published.

---

### Task 4: Make the Python client negotiate and terminate sessions correctly

**Files:**
- Modify: `app/llm/mcp_client.py:96-165,171-265,283-315,387-399`
- Modify: `tests/test_mcp_client.py`
- Modify: `tests/test_regressions.py:17-103`

**Interfaces:**
- Consumes: `DELETE /` and per-session routing from `mcp-remnawave:v3.2.1`.
- Produces: `HttpMcpClient.protocol_version: str | None`, subsequent `Mcp-Protocol-Version` headers, and `_terminate_session(): Awaitable[None]` used by `close()`.

- [ ] **Step 1: Write the failing negotiated-header test**

In every successful initialize response in `tests/test_mcp_client.py` and `tests/test_regressions.py`, include the negotiated version. Replace an empty result with the following object; when a fixture already has result fields such as `ok`, preserve them and add `protocolVersion`:

```python
"result": {"protocolVersion": "2025-11-25"}
```

Add to `tests/test_mcp_client.py`:

```python
@pytest.mark.asyncio
async def test_follow_up_requests_carry_the_negotiated_protocol_version() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-version"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        if body.get("method") == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = HttpMcpClient(base_url="http://mcp.test", http_client=http_client)
        assert await client.init() is True

    assert client.protocol_version == "2025-11-25"
    assert all(request.headers.get("mcp-protocol-version") == "2025-11-25" for request in seen[1:])
```

- [ ] **Step 2: Write the failing DELETE shutdown tests**

Add a helper test transport which checks `request.method` before parsing a JSON body, then add:

```python
@pytest.mark.asyncio
async def test_close_terminates_the_session_with_delete() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-close"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        if body.get("method") == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = HttpMcpClient(base_url="http://mcp.test", http_client=http_client)
        await client.init()
        await client.close()

    deletes = [request for request in seen if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].headers["mcp-session-id"] == "sess-close"
    assert deletes[0].headers["mcp-protocol-version"] == "2025-11-25"
    posted_methods = [
        json.loads(request.content).get("method") for request in seen if request.method == "POST"
    ]
    assert "notifications/cancelled" not in posted_methods
    assert client.session_id is None
    assert client.protocol_version is None
```

Parametrize compatibility responses:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("delete_status", [404, 405])
async def test_close_tolerates_an_absent_or_non_terminable_session(delete_status: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(delete_status)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-old-server"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )
        if body.get("method") == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = HttpMcpClient(base_url="http://mcp.test", http_client=http_client)
        await client.init()
        await client.close()

    assert client.initialized is False
```

- [ ] **Step 3: Write the failing no-blind-retry test**

The old client sends two initialize requests after `already initialized` without a usable session ID. Pin one attempt so a lost response cannot leak a second server session:

```python
@pytest.mark.asyncio
async def test_already_initialized_without_session_id_is_not_retried_blindly() -> None:
    initialize_calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal initialize_calls
        initialize_calls += 1
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Server already initialized"},
                "id": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = HttpMcpClient(base_url="http://mcp.test", http_client=http_client)
        assert await client.init() is False

    assert initialize_calls == 1
```

- [ ] **Step 4: Run the focused Python tests and verify red state**

```bash
uv run pytest tests/test_mcp_client.py tests/test_regressions.py -q
```

Expected: failures mention missing `protocol_version`, no DELETE request, and two initialize calls.

- [ ] **Step 5: Add protocol state and headers to `HttpMcpClient`**

Use current Streamable HTTP protocol metadata:

```python
PROTOCOL_VERSION = "2025-11-25"
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "Mcp-Protocol-Version"
```

Initialize state:

```python
self._session_id: str | None = None
self._protocol_version: str | None = None
```

Expose it read-only:

```python
@property
def protocol_version(self) -> str | None:
    """Protocol version negotiated with the active MCP session."""
    return self._protocol_version
```

Extend `_post` after the session header:

```python
if self._protocol_version:
    headers[self.PROTOCOL_HEADER] = self._protocol_version
```

After parsing a successful initialize response and before sending `notifications/initialized`, require and store the server's negotiated version:

```python
result = message.get("result")
if not isinstance(result, dict):
    raise McpException("MCP initialize response has no result object")
protocol_version = result.get("protocolVersion")
if not isinstance(protocol_version, str) or not protocol_version:
    raise McpException("MCP initialize response has no protocolVersion")
self._protocol_version = protocol_version
```

Remove the second `_initialize_session()` call from `init()`. If the server says `already initialized` without a session ID, return `False` after that one attempt and let the generic startup alert state that initialization failed.

The complete no-session branch in `init()` becomes:

```python
has_session = await self._initialize_session()
if not has_session:
    logger.error(
        "MCP initialization at %s did not yield a usable session; "
        "bot will run without tools from this server",
        self.base_url,
    )
    return False
```

Delete the stale “Restart the MCP server to clear” log text. Replace both debug/info statements that interpolate `self._session_id` with messages that say only `MCP session established` or `MCP existing session accepted`; never log the identifier value.

- [ ] **Step 6: Implement best-effort HTTP DELETE**

Add:

```python
async def _terminate_session(self) -> None:
    if self._session_id is None:
        return
    headers = {
        "Accept": "application/json, text/event-stream",
        self.SESSION_HEADER: self._session_id,
    }
    if self._protocol_version:
        headers[self.PROTOCOL_HEADER] = self._protocol_version

    response = await self._get_client().delete(self.base_url, headers=headers)
    if response.status_code in (200, 202, 204, 404, 405):
        return
    raise McpException(f"MCP session termination failed: {response.status_code} - {response.text}")
```

Reset all session-scoped state in both recovery and shutdown:

```python
self._session_id = None
self._protocol_version = None
self._initialized = False
```

Replace `close()` with:

```python
async def close(self) -> None:
    """Terminate the owned MCP session and close an internally owned HTTP client."""
    try:
        if self._initialized:
            await self._terminate_session()
    except Exception as error:
        logger.warning("Failed to terminate MCP session at %s: %s", self.base_url, error)
    finally:
        self.shutdown()

    if not self._custom_client and self._http_client is not None:
        await self._http_client.aclose()
        self._http_client = None
```

Delete the shutdown call to `notifications/cancelled`; keep `_send_notification` because it is still required for `notifications/initialized`.

- [ ] **Step 7: Align the production-wiring regression**

In `tests/test_regressions.py`, make `mcp_transport` return:

```python
"result": {"protocolVersion": "2025-11-25"}
```

Extend `test_session_id_is_propagated_after_the_handshake`:

```python
assert all(r.headers.get("mcp-protocol-version") == "2025-11-25" for r in follow_ups)
```

When tests explicitly call `close()`, make their mock transport branch on DELETE before `json.loads(request.content)`.

- [ ] **Step 8: Run the focused and complete bot gates**

```bash
uv run pytest tests/test_mcp_client.py tests/test_regressions.py -q
uv run ruff check app/llm/mcp_client.py tests/test_mcp_client.py tests/test_regressions.py
uv run ruff format --check app/llm/mcp_client.py tests/test_mcp_client.py tests/test_regressions.py
uv run mypy app
uv run pytest -q
```

Expected: all commands exit 0 and coverage remains at least 85%.

- [ ] **Step 9: Commit the client lifecycle**

```bash
git add app/llm/mcp_client.py tests/test_mcp_client.py tests/test_regressions.py
git commit -m "fix(mcp): terminate negotiated HTTP sessions"
```

---

### Task 5: Make startup diagnostics truthful

**Files:**
- Modify: `app/main.py:149-182`
- Modify: `tests/test_main.py:113-188`
- Modify: `tests/test_regressions.py:221-300`

**Interfaces:**
- Consumes: `HttpMcpClient.init() -> bool`, `McpRouter.list_tools() -> list[McpTool]`, and `AdminNotifier.notify_error(context, error=...)`.
- Produces: distinct admin contexts for handshake failure and a successfully initialized client with zero allowed tools.

- [ ] **Step 1: Make healthy lifecycle fixtures expose one allowed tool**

In the default `test_main_lifecycle` mock and `_stub_process_boundaries`, replace `list_tools.return_value = []` with:

```python
from app.llm.mcp_client import McpTool

mock_mcp.list_tools = MagicMock(return_value=[McpTool(name="nodes_list", description="List nodes")])
```

For `_stub_process_boundaries`, also set:

```python
bot.send_message = AsyncMock()
```

Return `bot` and `mcp_client` from the helper so the new assertions can inspect them.

- [ ] **Step 2: Write the two failing startup alert tests**

Add to `TestCompositionRoot`:

```python
@pytest.mark.asyncio
async def test_reports_a_handshake_failure_without_claiming_the_session_is_occupied(
    self, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    parts = _stub_process_boundaries(monkeypatch)
    parts["mcp_client"].init.return_value = False
    parts["mcp_client"].list_tools.return_value = []

    await main_module.main()

    text = parts["bot"].send_message.await_args.kwargs["text"]
    assert "не удалось инициализировать MCP" in text
    assert "занятую сессию" not in text
    assert MCP_URL in text


@pytest.mark.asyncio
async def test_reports_an_empty_allowed_tool_set_separately(
    self, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    parts = _stub_process_boundaries(monkeypatch)
    parts["mcp_client"].init.return_value = True
    parts["mcp_client"].list_tools.return_value = []

    await main_module.main()

    text = parts["bot"].send_message.await_args.kwargs["text"]
    assert "MCP вернул 0 разрешённых инструментов" in text
    assert "MCP_TAG" in text
    assert "REMNAWAVE_IS_SUPPORT" in text
```

- [ ] **Step 3: Run the tests and verify the old occupied-session copy fails them**

```bash
uv run pytest tests/test_main.py tests/test_regressions.py::TestCompositionRoot -q
```

Expected: the two new tests FAIL because `app/main.py` still emits “держит занятую сессию”.

- [ ] **Step 4: Split the startup branches in `main()`**

Capture the boolean and the exposed tools once:

```python
mcp_initialized = await mcp_client.init()
mcp_router = McpRouter(
    clients=[mcp_client],
    readonly=settings.remnawave_mcp_readonly,
    settings=settings,
)
exposed_tools = mcp_router.list_tools()

if not mcp_initialized:
    context = (
        "Бот запущен БЕЗ инструментов Remnawave: не удалось инициализировать "
        f"MCP ({settings.remnawave_mcp_url}). Проверьте доступность и логи "
        "контейнера mcp-remnawave."
    )
    logger.error(
        "Starting WITHOUT Remnawave tools: MCP initialization failed at %s",
        settings.remnawave_mcp_url,
    )
    await admin_notifier.notify_error(
        context,
        error=RuntimeError("MCP initialization failed"),
    )
elif not exposed_tools:
    context = (
        "Бот запущен БЕЗ инструментов Remnawave: MCP вернул 0 разрешённых "
        "инструментов. Проверьте MCP_TAG, REMNAWAVE_IS_SUPPORT и allowlist бота "
        f"({settings.remnawave_mcp_url})."
    )
    logger.error(
        "Starting WITHOUT Remnawave tools: MCP exposed no allowed tools at %s",
        settings.remnawave_mcp_url,
    )
    await admin_notifier.notify_error(
        context,
        error=RuntimeError("no allowed MCP tools exposed"),
    )
```

Remove the comment claiming `mcp-remnawave` has one global session and remove the instruction to restart both containers.

- [ ] **Step 5: Run startup and full gates**

```bash
uv run pytest tests/test_main.py tests/test_regressions.py::TestCompositionRoot -q
uv run ruff check app/main.py tests/test_main.py tests/test_regressions.py
uv run ruff format --check app/main.py tests/test_main.py tests/test_regressions.py
uv run mypy app
uv run pytest -q
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the diagnostic change**

```bash
git add app/main.py tests/test_main.py tests/test_regressions.py
git commit -m "fix(mcp): report startup failures accurately"
```

---

### Task 6: Pin the fixed image and remove coupled deploy restarts

**Files:**
- Modify: `docker-compose.yml:21-23`
- Modify: `.env.example:44-47`
- Modify: `.github/workflows/deploy.yml:22-33`
- Modify: `README.md:52-62,98-109,152-158`
- Modify: `CI-CD.md:54-80`

**Interfaces:**
- Consumes: published image `mitetenov/remnawave-mcp:v3.2.1` from Task 3 and DELETE behavior from Task 4.
- Produces: default `MCP_TAG=v3.2.1` and a bot-only deploy command that leaves MCP/PostgreSQL container identities unchanged.

- [ ] **Step 1: Verify the immutable MCP image exists before changing the pin**

Run from a machine authenticated to Docker Hub if the repository is private:

```bash
docker buildx imagetools inspect mitetenov/remnawave-mcp:v3.2.1
```

Expected: exit 0 and a manifest digest. If the tag is absent, stop here; do not merge a broken default.

- [ ] **Step 2: Update the pinned tag everywhere**

Change `docker-compose.yml`:

```yaml
image: mitetenov/remnawave-mcp:${MCP_TAG:-v3.2.1}
```

Change `.env.example`:

```dotenv
MCP_TAG=v3.2.1
```

Change the README default table row:

```markdown
| `MCP_TAG` | `v3.2.1` | Тег образа интеграции с Remnawave |
```

Search for stale pins:

```bash
rg -n "v3\.2\.0|single global session|занятую сессию|restart.*mcp|перезапустите.*mcp" docker-compose.yml .env.example README.md CI-CD.md .github app tests
```

Expected after all planned edits: no operational instruction or default still treats a coupled restart as the normal solution. Historical plan/spec documents may retain the incident wording.

- [ ] **Step 3: Restrict the deploy workflow to the bot service**

Replace the workflow's pull/up block with:

```yaml
export BOT_TAG=${{ inputs.tag }}
docker compose pull support-bot
docker compose up -d --wait support-bot
```

Delete the four-line `--force-recreate` comment. Do not add `--no-deps`: Compose should still check/start declared dependencies when they are absent, but it must not recreate healthy dependencies.

- [ ] **Step 4: Add the operational migration note**

Add a short “Обновление MCP 3.2.0 → 3.2.1” section to README and CI-CD with this exact order:

```bash
cd /root/supportBot
cp .env .env.pre-mcp-3.2.1
sed -i 's/^MCP_TAG=.*/MCP_TAG=v3.2.1/' .env
docker compose pull mcp-remnawave
docker compose up -d --wait mcp-remnawave
docker compose pull support-bot
docker compose up -d --wait support-bot
```

State explicitly:

- the one-time MCP upgrade clears the old singleton session;
- after both versions are deployed, `docker compose restart support-bot` is safe and must not restart MCP;
- while still on v3.2.0, the emergency recovery remains `docker compose up -d --force-recreate mcp-remnawave support-bot`, but it is not the steady-state deployment procedure.

Change the FAQ-only rebuild example from `--force-recreate support-bot` to:

```bash
docker compose up -d --wait support-bot
```

- [ ] **Step 5: Validate Compose and documentation consistency**

Use the example environment only for interpolation validation:

```bash
docker compose --env-file .env.example config --images
```

Expected output contains:

```text
mitetenov/remnawave-mcp:v3.2.1
```

Run:

```bash
rg -n "MCP_TAG|force-recreate|v3\.2\.1" docker-compose.yml .env.example README.md CI-CD.md .github/workflows/deploy.yml
```

Review every match; `force-recreate` may appear only in the explicitly labeled v3.2.0 emergency recovery note.

- [ ] **Step 6: Run the repository gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q
```

Expected: all commands exit 0 and coverage stays at least 85%.

- [ ] **Step 7: Commit deploy and documentation changes**

```bash
git add docker-compose.yml .env.example .github/workflows/deploy.yml README.md CI-CD.md
git commit -m "chore(mcp): deploy session-safe server independently"
```

---

### Task 7: Perform staged rollout and restart acceptance test

**Files:**
- No repository files unless the verification reveals a reproducible defect; fix such a defect in its owning task and rerun that task's tests before continuing.

**Interfaces:**
- Consumes: `mcp-remnawave:v3.2.1`, the updated SupportAiBot image, Docker Compose service names `mcp-remnawave` and `support-bot`, and the admin support chat.
- Produces: production evidence that bot-only restart, MCP-only restart, DELETE cleanup, and session recovery all work.

- [ ] **Step 1: Record the pre-deploy state**

```bash
cd /root/supportBot
docker compose ps
docker inspect -f '{{.Id}} {{.Config.Image}}' vpn-support-mcp
docker inspect -f '{{.Id}} {{.Config.Image}}' vpn-support-bot
```

Save the output in the deployment log or release notes; do not paste environment variables or session IDs.

- [ ] **Step 2: Deploy MCP 3.2.1 first, then the updated bot**

```bash
cp .env .env.pre-mcp-3.2.1
sed -i 's/^MCP_TAG=.*/MCP_TAG=v3.2.1/' .env
docker compose pull mcp-remnawave
docker compose up -d --wait mcp-remnawave
docker compose pull support-bot
docker compose up -d --wait support-bot
```

Expected: both services healthy; PostgreSQL is not recreated.

- [ ] **Step 3: Verify healthy tool discovery without exposing session IDs**

```bash
docker compose logs --since=5m support-bot | rg "MCP HTTP client initialized with [1-9][0-9]* tools"
if docker compose logs --since=5m support-bot | rg -q "no MCP tools exposed|занятую сессию|MCP initialization failed"; then exit 1; fi
```

Expected: the first command finds one healthy initialization line; the second command finds no matches.

- [ ] **Step 4: Restart only the bot and prove MCP was not recreated**

```bash
before_mcp_id=$(docker inspect -f '{{.Id}}' vpn-support-mcp)
docker compose restart support-bot
docker compose up -d --wait support-bot
after_mcp_id=$(docker inspect -f '{{.Id}}' vpn-support-mcp)
test "$before_mcp_id" = "$after_mcp_id"
docker compose logs --since=2m support-bot | rg "MCP HTTP client initialized with [1-9][0-9]* tools"
```

Expected: the ID comparison exits 0 and the replacement bot loads tools.

- [ ] **Step 5: Confirm the old session was deleted**

```bash
if docker compose logs --since=3m mcp-remnawave | rg -q "already initialized|Server not initialized"; then exit 1; fi
```

Expected: no matches. The implementation deliberately does not log session IDs, so absence of errors plus successful fresh initialization is the observable proof.

- [ ] **Step 6: Restart only MCP and exercise existing client recovery**

```bash
before_bot_id=$(docker inspect -f '{{.Id}}' vpn-support-bot)
docker compose restart mcp-remnawave
docker compose up -d --wait mcp-remnawave
after_bot_id=$(docker inspect -f '{{.Id}}' vpn-support-bot)
test "$before_bot_id" = "$after_bot_id"
```

Send one support request that necessarily uses Remnawave data, for example an operator `/ask` asking to inspect a known test user's subscription. Then run:

```bash
docker compose logs --since=3m support-bot | rg "expired — re-initializing|MCP HTTP client initialized with [1-9][0-9]* tools"
```

Expected: one reinitialization sequence and a successful answer backed by the tool result; no occupied-session alert in the support chat.

- [ ] **Step 7: Verify the ordinary deploy workflow leaves dependencies untouched**

Run the manual Deploy workflow once with the already deployed bot tag. Compare container IDs before and after:

```bash
docker inspect -f '{{.Id}}' vpn-support-mcp
docker inspect -f '{{.Id}}' vpn-support-pg
```

Expected: MCP and PostgreSQL IDs do not change. The support-bot ID may change when its image/tag changes.

- [ ] **Step 8: Close the incident**

Record these four facts in the release/incident note:

```text
- mcp-remnawave v3.2.1 deployed before the updated bot
- bot-only restart preserved the MCP container and rediscovered tools
- MCP-only restart preserved the bot container and recovered on the next tool call
- no "no MCP tools exposed" admin alert was emitted
```

Do not delete `.env.pre-mcp-3.2.1` during this task; it is the rollback copy and can be removed later under the operator's retention policy.

---

## Rollback

If `mcp-remnawave:v3.2.1` fails before the bot client is deployed:

```bash
cd /root/supportBot
cp .env.pre-mcp-3.2.1 .env
docker compose pull mcp-remnawave
docker compose up -d --force-recreate mcp-remnawave support-bot
```

If the updated bot fails but MCP 3.2.1 is healthy, set `PREVIOUS_BOT_TAG` to the immutable bot tag recorded in Task 7 Step 1 and roll back only `BOT_TAG`. Keep MCP 3.2.1 because it is backward compatible with the old client and removes the singleton initialization failure:

```bash
BOT_TAG="$PREVIOUS_BOT_TAG" docker compose pull support-bot
BOT_TAG="$PREVIOUS_BOT_TAG" docker compose up -d --wait support-bot
```

Never roll back PostgreSQL for this incident; there is no schema change.
