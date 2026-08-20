# VPN Support Bot — Python Migration & Optimization Design

- **Date**: 2026-08-20
- **Status**: Approved
- **Goal**: Rewrite the VPN Support Telegram Bot from Java 21 / Spring Boot 3 to Python 3.12+ async stack, optimizing memory usage (35–50 MB vs 250+ MB), startup time (<0.5s vs 5–8s), Docker image size (~75 MB vs 237 MB) and build time (<10s vs 1.5–3 min) while preserving 100% functional parity and enterprise-grade architecture.

---

## 1. Overview & Architecture

The Python implementation replaces the Spring Boot service with an asynchronous service based on:
- **Telegram Bot Framework**: `aiogram 3.x` (typed, async/await routing with Dispatcher & Routers).
- **HTTP Client**: `httpx.AsyncClient` with connection pooling for LLM API and MCP JSON-RPC calls.
- **Database & Storage**: `asyncpg` + `SQLAlchemy 2.0 (async)` for ORM models, with direct optimized SQL queries for PGVector hybrid search (cosine distance + tsvector FTS + Reciprocal Rank Fusion).
- **Healthcheck & Monitoring**: Lightweight HTTP server (`aiohttp.web`) exposing `/actuator/health` and `/health` on port 8080 for Docker compatibility.
- **Packaging & Build**: `pyproject.toml` managed via `uv` in a multi-stage Docker build (`python:3.12-slim`).

```
                              ┌────────────────────────────────────────┐
                              │           Telegram Updates             │
                              └──────────────────┬─────────────────────┘
                                                 │
                                     ┌───────────▼───────────┐
                                     │     aiogram Router    │
                                     └───────────┬───────────┘
                                                 │
                                ┌────────────────▼────────────────┐
                                │       UserMessageBuffer         │ (coalesces 2.5s / 5 msgs)
                                └────────────────┬────────────────┘
                                                 │
                                ┌────────────────▼────────────────┐
                                │      UserMessagePipeline        │
                                └───┬────────────┬────────────┬───┘
                                    │            │            │
                     ┌──────────────▼───┐ ┌──────▼──────┐ ┌───▼─────────────┐
                     │ ConversationState│ │ RateLimiter │ │ TypingIndicator │
                     └──────────────────┘ └─────────────┘ └─────────────────┘
                                    │            │            │
                                ┌───▼────────────▼────────────▼───┐
                                │   FaqEmbeddingService (RAG)     │
                                │   (PGVector + FTS + RRF Cache)  │
                                └────────────────┬────────────────┘
                                                 │
                                ┌────────────────▼────────────────┐
                                │      LlmClient (Strategy)       │
                                │  (Gemini / DeepSeek / OpenAI)   │
                                └────────────────┬────────────────┘
                                                 │
                                ┌────────────────▼────────────────┐
                                │       McpRouter (Gatekeeper)    │
                                │ (Allow-list & telegram_id pin)  │
                                └────────────────┬────────────────┘
                                                 │
                                ┌────────────────▼────────────────┐
                                │     SupportGroupForwarder       │
                                │    & KnowledgeGapService        │
                                └─────────────────────────────────┘
```

---

## 2. Directory & Module Structure

```
support-bot/
├── pyproject.toml              # Project dependencies & tool configurations
├── Dockerfile                  # Multi-stage optimized build using uv
├── docker-compose.yml          # Compose specification matching current services
├── faq/
│   └── faq.json                # FAQ database with questions, answers, keywords
├── app/
│   ├── __init__.py
│   ├── main.py                 # Composition Root: DI, polling, healthcheck, graceful shutdown
│   ├── config.py               # Pydantic Settings: environment variables & validation
│   ├── constants.py            # Localized strings, regexes, system prompts
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── router.py           # aiogram Router for text, commands, media, reactions
│   │   ├── buffer.py           # UserMessageBuffer: message debouncing & batching
│   │   ├── pipeline.py         # UserMessagePipeline: orchestration of response lifecycle
│   │   ├── rate_limiter.py     # UserRateLimiter: sliding window / leaky bucket per user
│   │   ├── forwarder.py        # SupportGroupForwarder: forum topic sync & operator replies
│   │   ├── topic_manager.py    # TopicManager: Telegram forum topic creation & lock handling
│   │   ├── typing.py           # TypingIndicator: async background typing action session
│   │   ├── command_handler.py  # SupportCommandHandler: /start, /help, /operator, /stats, /gaps
│   │   ├── photo_downloader.py # PhotoDownloader: fetch Telegram photo as base64
│   │   └── admin_notifier.py   # AdminNotifier: error notifications to admin/group
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py             # AbstractLlmClient: multi-turn tool calling loop & usage tracking
│   │   ├── gemini.py           # GeminiClient: Gemini generateContent + tool calling + vision
│   │   ├── deepseek.py         # DeepSeekClient: DeepSeek OpenAI-compatible chat completions
│   │   ├── openai_client.py    # OpenAiClient: OpenAI Responses API + tool calling + vision
│   │   ├── prompt.py           # SupportPrompt: system prompt builder with FAQ and sender ID
│   │   ├── escalation.py       # EscalationPolicy: [ESCALATE] marker & regex detection
│   │   ├── rejection.py        # RejectionDetector: detects user rejecting previous answer
│   │   ├── mcp_client.py       # HttpMcpClient: JSON-RPC 2.0 over HTTP with session handling
│   │   └── mcp_router.py       # McpRouter: tool allow-list and telegram_id enforcement
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── embedding.py        # EmbeddingProvider (Gemini: 2000d, OpenAI: 1536d)
│   │   ├── service.py          # FaqEmbeddingService: hybrid search, RRF rank fusion, LRU cache
│   │   ├── initializer.py      # FaqInitializer: SHA-256 hash validation & auto-indexing
│   │   └── knowledge_gaps.py   # KnowledgeGapService: gap tracking & vector deduplication
│   └── storage/
│       ├── __init__.py
│       ├── database.py         # SQLAlchemy 2.0 async engine & sessionmaker
│       ├── models.py           # ORM models (User, TopicMapping, MessageMapping, ChatMessage, TokenUsage, KnowledgeGap)
│       └── chat_history.py     # ChatHistoryService: Deque buffer + async DB persistence + TTL
└── tests/                      # pytest test suite
```

---

## 3. Detailed Component Specifications

### 3.1 Configuration & Startup Validation (`app/config.py`)
- Uses `pydantic-settings` to load and validate all `.env` variables:
  - `LLM_PROVIDER`: `deepseek`, `gemini`, or `openai` (defaults to `deepseek`).
  - `EMBEDDING_PROVIDER`: `gemini` or `openai` (defaults to `gemini`).
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SUPPORT_GROUP_CHAT_ID` (validated: negative int).
  - `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` (validated when `LLM_PROVIDER=deepseek`).
  - `GEMINI_API_KEY`, `GEMINI_MODEL` (validated when `LLM_PROVIDER=gemini` or `EMBEDDING_PROVIDER=gemini`).
  - `OPENAI_API_KEY` (must start with `sk-`), `OPENAI_MODEL`, `OPENAI_EMBEDDING_MODEL` (validated when active).
  - `REMNAWAVE_MCP_URL`, `REMNAWAVE_MCP_READONLY`.
  - `PGVECTOR_HOST`, `PGVECTOR_PORT`, `PGVECTOR_USER`, `PGVECTOR_PASSWORD`, `PGVECTOR_DB`.
  - Debounce window (2.5s), max messages (5), history max (20), operator suppression window (30m).

### 3.2 Telegram Routing & Pipelines (`app/bot/`)
- **`router.py`**:
  - Handles `/start`, `/help`, `/operator`, `/stats`, `/gaps`.
  - Filters out bot messages.
  - Distinguishes messages from support group vs direct user messages.
  - Support group messages: if in a topic, forwards reply/media back to the user (`copy_message` or `send_message`), adds delivery confirmation, marks operator activity in `ConversationState`.
  - User messages: text is buffered in `UserMessageBuffer`; photos are checked for vision support, downloaded, and buffered; unsupported media receives explanation and is forwarded to support.
  - Message reactions: synchronized bidirectionally between user chat and support group topic using `MessageMapping`.
- **`buffer.py` (`UserMessageBuffer`)**:
  - Implements debounce queue: holds incoming messages per user ID.
  - Sets timer for 2.5s (`asyncio.get_running_loop().call_later`); if new messages arrive, resets or triggers immediately if buffer length reaches 5.
  - Passes coalesced `MessageBatch` to `UserMessagePipeline`.
- **`pipeline.py` (`UserMessagePipeline`)**:
  - Checks if operator is active (`ConversationState.is_operator_recently_active`) -> if active, forwards user message to topic with `support.ai.suppressed` and skips LLM.
  - Checks rate limiter -> if tripped, notifies user and forwards to support with `support.ratelimited`.
  - Starts `TypingIndicator` session.
  - Invokes `LlmClient.chat()` or `LlmClient.chat_with_image()`.
  - Strips `[ESCALATE]` marker, sends reply to user.
  - Checks escalation triggers (`modelRequestedEscalation` or `userRequestsHuman`) -> forwards to support forum topic with `@admin` tag if needed.
  - Evaluates knowledge gaps via `KnowledgeGapService`.
- **`topic_manager.py` (`TopicManager`)**:
  - Resolves or creates Telegram forum topic for user (`create_forum_topic`), storing `TopicMapping`.
  - Uses `asyncio.Lock` per user to avoid race conditions when concurrent messages arrive for a new user.
  - Handles stale/deleted topics by recreating and updating mapping.

### 3.3 LLM & MCP Integration (`app/llm/`)
- **`AbstractLlmClient`**:
  - Coordinates multi-turn conversation loop (up to 5 iterations).
  - Rewrites queries for contextual follow-ups ("а на айфоне?") using regex patterns for Russian anaphora/particles.
  - Invokes `FaqEmbeddingService.build_faq_context(query, rejected_faqs)`.
  - Calls provider API (`GeminiClient`, `DeepSeekClient`, `OpenAiClient`).
  - If response has tool calls, executes them via `McpRouter.call_tool()`, appends tool output to conversation, and repeats.
  - Saves token usage to `LlmTokenUsageRepository`.
- **`McpRouter`**:
  - Allow-lists read tools (`users_get_by_telegram_id`, `nodes_list`, `nodes_get`, `hwid_devices_list`) and write tool (`hwid_device_delete` only if `REMNAWAVE_MCP_READONLY=false`).
  - Enforces sender security: detects Telegram ID parameter name in tool schema (e.g. `telegramId`, `telegram_id`) and overrides its value with actual authenticated sender's ID, preventing prompt injection attacks.
- **`HttpMcpClient`**:
  - Implements MCP HTTP transport (JSON-RPC 2.0): initializes session, handles `Mcp-Session-Id` header, parses SSE payloads (`data: {...}`), queries `tools/list` and executes `tools/call`.

### 3.4 RAG & Storage Layer (`app/rag/`, `app/storage/`)
- **`FaqEmbeddingService`**:
  - Generates query vector via `EmbeddingProvider` (Gemini: 2000 dim, OpenAI: 1536 dim).
  - In-memory LRU cache (max 256 entries) for query embeddings.
  - Executes hybrid search query with Reciprocal Rank Fusion (RRF constant $k=60$):
    - Vector similarity: $1 - (\text{embedding} \Leftrightarrow \text{query\_vector})$
    - FTS rank: `ts_rank(to_tsvector('russian', question || ' ' || COALESCE(keywords, '') || ' ' || answer), websearch_to_tsquery('russian', clean_query))`
    - Floor filters: vector similarity $\ge 0.65$, FTS rank $\ge 0.01$.
    - Fallback searches for connection problems and referral questions.
- **`FaqInitializer`**:
  - Computes SHA-256 hash of `faq/faq.json`.
  - Compares with `faq_metadata` table. If matches and FAQ count > 0, skips re-indexing.
  - Otherwise drops and re-indexes FAQ rows and HNSW / GIN indexes.
- **`KnowledgeGapService`**:
  - Identifies unresolved queries (`NO_MATCH`, `LOW_SIMILARITY`, `ESCALATED`, `LLM_UNSURE`, `USER_OPERATOR`).
  - Deduplicates similar gaps using cosine similarity $\ge 0.85$ on existing vector embeddings in `knowledge_gaps` table.
- **`ChatHistoryService`**:
  - Per-user sliding in-memory `deque` of recent messages (max 20).
  - Asynchronous background write to Postgres `chat_messages` table.
  - Hourly cleanup task for records older than TTL (7 days).

---

## 4. Docker & Resource Optimization

### Multi-stage Dockerfile Design:
```dockerfile
FROM ghcr.io/astral-sh/uv:latest AS uv-bin

FROM python:3.12-slim AS builder
WORKDIR /app
COPY --from=uv-bin /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install -r pyproject.toml

FROM python:3.12-slim AS runtime
WORKDIR /app

RUN groupadd -r bot && useradd -r -g bot bot && \
    apt-get update && apt-get install -y --no-install-recommends wget && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder --chown=bot:bot /opt/venv /opt/venv
COPY --chown=bot:bot faq /app/faq
COPY --chown=bot:bot app /app/app

USER bot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD wget -q --spider http://localhost:8080/health || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
```

---

## 5. Verification Plan

1. **Unit & Async Tests (`pytest`)**:
   - `test_buffer.py`: Debounce window, max message threshold, batch ordering.
   - `test_rate_limiter.py`: Interval enforcement, stale entry eviction.
   - `test_escalation.py`: Regex word boundaries (Russian morphology), marker stripping.
   - `test_mcp_router.py`: Tool filtering, readonly mode, Telegram ID injection override.
   - `test_mcp_client.py`: JSON-RPC request formatting, session header, SSE parsing.
   - `test_llm_clients.py`: Gemini, DeepSeek, OpenAI request/response formatting, tool loop iterations.
   - `test_rag_service.py`: RRF scoring, fallback merging, LRU embedding cache.
   - `test_command_handler.py`: Admin auth check, token formatting, top gaps listing.
2. **Local Environment Run**:
   - Verify linting / type checking (`ruff check`, `mypy`).
   - Run full test suite with `pytest`.
3. **Docker Build Verification**:
   - Build image locally to verify image size (<100 MB) and build speed (<10s).
