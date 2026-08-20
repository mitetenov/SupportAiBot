# VPN Support Bot — Python Migration & Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the VPN Support Telegram Bot from Java 21 / Spring Boot 3 to Python 3.12+ async stack (aiogram 3, asyncpg, SQLAlchemy 2.0 async, httpx, uv), optimizing memory usage (35–50 MB vs 250+ MB), startup time (<0.5s), image size (~75 MB), and build time (<10s) with 100% test coverage and full functional parity.

**Architecture:** Clean modular architecture with explicit composition root in `app/main.py`, separation into `app/bot/` (Telegram routing, buffering, forwarding), `app/llm/` (Gemini, DeepSeek, OpenAI, MCP router/client), `app/rag/` (PGVector, FTS, RRF, LRU cache, FAQ initializer), `app/storage/` (Postgres models, async repositories, chat history), and `app/config.py` (Pydantic settings validation).

**Tech Stack:** Python 3.12, aiogram 3.17+, SQLAlchemy 2.0+ (async), asyncpg 0.30+, pgvector-python 0.3.6+, httpx 0.28+, pydantic-settings 2.7+, aiohttp 3.11+ (healthcheck server), uv, pytest, pytest-asyncio, pytest-mock.

## Global Constraints
- Target branch: local `python-migration` branch only (do not push to remote).
- Python 3.12+ compatibility with strict type annotations.
- Full parity with all 36 test scenarios of original Java codebase.
- Backward-compatible with existing PostgreSQL 17 + PGVector database schema and `.env` variables.
- HTTP Healthcheck endpoint exposed on port 8080 at `/actuator/health` and `/health`.

---

### Task 1: Scaffolding, Configuration & Constants

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/constants.py`
- Create: `app/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `app.config.Settings`, `app.config.get_settings()`, `app.constants.MESSAGES`, `app.constants.SupportPrompt`, `app.constants.EscalationRegexes`.

- [ ] **Step 1: Write `pyproject.toml`**
Define dependencies: `aiogram`, `pydantic-settings`, `httpx`, `asyncpg`, `sqlalchemy`, `pgvector`, `aiohttp`, `pytest`, `pytest-asyncio`, `pytest-mock`, `ruff`.

- [ ] **Step 2: Write failing test for Settings and Startup Validation**
Write `tests/test_config.py` testing validation of Telegram bot token, negative group ID, LLM provider checks (DeepSeek requires API key and model, OpenAI requires `sk-` prefix, Gemini requires API key and model), embedding provider validation, and Remnawave MCP URL requirement.

- [ ] **Step 3: Run `pytest tests/test_config.py` and verify FAIL**

- [ ] **Step 4: Implement `app/constants.py` and `app/config.py`**
Implement all Russian string templates from `messages.properties` in `app/constants.py`, and implement `Settings` with `@model_validator(mode="after")` in `app/config.py`.

- [ ] **Step 5: Run `pytest tests/test_config.py` and verify PASS**

- [ ] **Step 6: Commit**
```bash
git add pyproject.toml app/ tests/
git commit -m "feat(config): add project configuration, settings validation, and constants"
```

---

### Task 2: Storage Layer & Chat History

**Files:**
- Create: `app/storage/__init__.py`
- Create: `app/storage/database.py`
- Create: `app/storage/models.py`
- Create: `app/storage/chat_history.py`
- Create: `tests/test_chat_history.py`
- Create: `tests/test_storage_models.py`

**Interfaces:**
- Consumes: `app.config.Settings`
- Produces: `app.storage.database.DatabaseSessionManager`, `app.storage.models.*`, `app.storage.chat_history.ChatHistoryService`.

- [ ] **Step 1: Write failing tests for `ChatHistoryService` and Storage Models**
Write `tests/test_chat_history.py` and `tests/test_storage_models.py` testing user/assistant message appending, max message trimming (20 msgs), `to_gemini_contents` formatting, rejected FAQ tracking, and TTL eviction.

- [ ] **Step 2: Run `pytest tests/test_chat_history.py tests/test_storage_models.py` and verify FAIL**

- [ ] **Step 3: Implement `database.py`, `models.py`, and `chat_history.py`**
Implement SQLAlchemy 2.0 async models for `users`, `topic_mappings`, `message_mappings`, `chat_messages`, `llm_token_usage`, `knowledge_gaps`, `faq`, `faq_metadata`. Implement in-memory `ChatHistoryService` with asynchronous persistence and eviction.

- [ ] **Step 4: Run `pytest tests/test_chat_history.py tests/test_storage_models.py` and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/storage/ tests/test_chat_history.py tests/test_storage_models.py
git commit -m "feat(storage): implement models, database connection manager, and chat history service"
```

---

### Task 3: Domain Policies, Detectors & State Management

**Files:**
- Create: `app/llm/__init__.py`
- Create: `app/llm/escalation.py`
- Create: `app/llm/rejection.py`
- Create: `app/llm/prompt.py`
- Create: `app/bot/__init__.py`
- Create: `app/bot/rate_limiter.py`
- Create: `app/bot/conversation_state.py`
- Create: `tests/test_escalation.py`
- Create: `tests/test_rejection.py`
- Create: `tests/test_prompt.py`
- Create: `tests/test_rate_limiter.py`
- Create: `tests/test_conversation_state.py`

**Interfaces:**
- Produces: `EscalationPolicy`, `RejectionDetector`, `SupportPrompt`, `UserRateLimiter`, `ConversationState`.

- [ ] **Step 1: Write failing tests for escalation, rejection, prompt, rate limiter, and conversation state**
Test Russian morphology word boundary matching (prevent false positives on "живу в Германии"), marker stripping, rejection phrases ("не то", "не подходит"), 3s rate limit enforcement, and operator activity suppression window (30m).

- [ ] **Step 2: Run tests and verify FAIL**

- [ ] **Step 3: Implement domain components**
Implement `EscalationPolicy`, `RejectionDetector`, `SupportPrompt`, `UserRateLimiter`, `ConversationState`.

- [ ] **Step 4: Run tests and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/llm/escalation.py app/llm/rejection.py app/llm/prompt.py app/bot/rate_limiter.py app/bot/conversation_state.py tests/
git commit -m "feat(domain): add escalation policy, rejection detector, prompts, rate limiter, and conversation state"
```

---

### Task 4: MCP Client & McpRouter

**Files:**
- Create: `app/llm/mcp_client.py`
- Create: `app/llm/mcp_router.py`
- Create: `tests/test_mcp_client.py`
- Create: `tests/test_mcp_router.py`

**Interfaces:**
- Consumes: `app.config.Settings`, `httpx.AsyncClient`
- Produces: `HttpMcpClient`, `McpRouter`, `McpTool` dataclass.

- [ ] **Step 1: Write failing tests for `HttpMcpClient` and `McpRouter`**
Test JSON-RPC 2.0 initialize protocol, session ID header extraction, SSE response parsing, allow-list filtering (5 tools), readonly write-tool withholding, and Telegram ID parameter injection security override.

- [ ] **Step 2: Run `pytest tests/test_mcp_client.py tests/test_mcp_router.py` and verify FAIL**

- [ ] **Step 3: Implement `HttpMcpClient` and `McpRouter`**
Implement HTTP JSON-RPC 2.0 client with session reuse, tool schema inspection, and `McpRouter` parameter coercion.

- [ ] **Step 4: Run tests and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/llm/mcp_client.py app/llm/mcp_router.py tests/test_mcp_client.py tests/test_mcp_router.py
git commit -m "feat(mcp): implement HTTP MCP client and security McpRouter"
```

---

### Task 5: RAG, PGVector Hybrid Search, FAQ Initializer & Knowledge Gaps

**Files:**
- Create: `app/rag/__init__.py`
- Create: `app/rag/embedding.py`
- Create: `app/rag/service.py`
- Create: `app/rag/initializer.py`
- Create: `app/rag/knowledge_gaps.py`
- Create: `tests/test_embedding.py`
- Create: `tests/test_faq_service.py`
- Create: `tests/test_faq_initializer.py`
- Create: `tests/test_knowledge_gaps.py`

**Interfaces:**
- Consumes: `app.storage.database.DatabaseSessionManager`, `app.config.Settings`, `httpx.AsyncClient`
- Produces: `EmbeddingProvider`, `GeminiEmbeddingProvider`, `OpenAiEmbeddingProvider`, `FaqEmbeddingService`, `FaqInitializer`, `KnowledgeGapService`.

- [ ] **Step 1: Write failing tests for embeddings, RAG hybrid search, FAQ initializer, and knowledge gaps**
Test vector embedding dimension validation, RRF ranking calculation, query embedding LRU cache, SHA-256 hash skip logic on unchanged FAQ, and cosine deduplication for knowledge gaps.

- [ ] **Step 2: Run tests and verify FAIL**

- [ ] **Step 3: Implement RAG and Knowledge Gap components**
Implement embedding providers, `FaqEmbeddingService` with asyncpg hybrid search and RRF, `FaqInitializer` with `faq/faq.json`, and `KnowledgeGapService`.

- [ ] **Step 4: Run tests and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/rag/ tests/test_embedding.py tests/test_faq_service.py tests/test_faq_initializer.py tests/test_knowledge_gaps.py
git commit -m "feat(rag): implement PGVector hybrid search, RRF ranking, FAQ indexing, and knowledge gaps"
```

---

### Task 6: LLM Client Implementations (Gemini, DeepSeek, OpenAI)

**Files:**
- Create: `app/llm/base.py`
- Create: `app/llm/gemini.py`
- Create: `app/llm/deepseek.py`
- Create: `app/llm/openai_client.py`
- Create: `tests/test_abstract_llm_client.py`
- Create: `tests/test_gemini_client.py`
- Create: `tests/test_deepseek_client.py`
- Create: `tests/test_openai_client.py`
- Create: `tests/test_contextual_query.py`

**Interfaces:**
- Consumes: `app.llm.mcp_router.McpRouter`, `app.rag.service.FaqEmbeddingService`, `app.storage.chat_history.ChatHistoryService`
- Produces: `LlmClient`, `AbstractLlmClient`, `GeminiClient`, `DeepSeekClient`, `OpenAiClient`.

- [ ] **Step 1: Write failing tests for LLM clients**
Test `build_contextual_search_query` for Russian follow-ups, multi-turn tool calling loop execution (up to 5 iterations), schema parameter sanitization for Gemini, OpenAI Responses API format, token usage recording, and image support exceptions.

- [ ] **Step 2: Run tests and verify FAIL**

- [ ] **Step 3: Implement `base.py`, `gemini.py`, `deepseek.py`, and `openai_client.py`**
Implement the Strategy and Template Method patterns for all three LLM providers.

- [ ] **Step 4: Run tests and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/llm/ tests/test_abstract_llm_client.py tests/test_gemini_client.py tests/test_deepseek_client.py tests/test_openai_client.py tests/test_contextual_query.py
git commit -m "feat(llm): implement Gemini, DeepSeek, and OpenAI LLM clients with multi-turn tool calling"
```

---

### Task 7: Telegram Handlers, Buffering, Forwarding & Pipeline

**Files:**
- Create: `app/bot/buffer.py`
- Create: `app/bot/typing.py`
- Create: `app/bot/photo_downloader.py`
- Create: `app/bot/topic_manager.py`
- Create: `app/bot/forwarder.py`
- Create: `app/bot/command_handler.py`
- Create: `app/bot/admin_notifier.py`
- Create: `app/bot/pipeline.py`
- Create: `app/bot/router.py`
- Create: `tests/test_buffer.py`
- Create: `tests/test_typing.py`
- Create: `tests/test_photo_downloader.py`
- Create: `tests/test_topic_manager.py`
- Create: `tests/test_forwarder.py`
- Create: `tests/test_command_handler.py`
- Create: `tests/test_pipeline.py`
- Create: `tests/test_router.py`

**Interfaces:**
- Consumes: `aiogram.Bot`, `aiogram.Dispatcher`, `app.llm.base.LlmClient`, `app.storage.database.DatabaseSessionManager`
- Produces: `UserMessageBuffer`, `TypingIndicator`, `PhotoDownloader`, `TopicManager`, `SupportGroupForwarder`, `SupportCommandHandler`, `UserMessagePipeline`, `setup_router()`.

- [ ] **Step 1: Write failing tests for Telegram components**
Test message debouncing (2.5s window / 5 max msgs), topic creation and locking, support group reply copying, command permissions (`/stats`, `/gaps`), pipeline error handling and escalation tagging.

- [ ] **Step 2: Run tests and verify FAIL**

- [ ] **Step 3: Implement all Telegram components and router**
Implement `UserMessageBuffer`, `TypingIndicator`, `PhotoDownloader`, `TopicManager`, `SupportGroupForwarder`, `SupportCommandHandler`, `AdminNotifier`, `UserMessagePipeline`, and aiogram `router.py`.

- [ ] **Step 4: Run tests and verify PASS**

- [ ] **Step 5: Commit**
```bash
git add app/bot/ tests/test_buffer.py tests/test_typing.py tests/test_photo_downloader.py tests/test_topic_manager.py tests/test_forwarder.py tests/test_command_handler.py tests/test_pipeline.py tests/test_router.py
git commit -m "feat(bot): implement message buffering, pipeline, forwarding, commands, and aiogram router"
```

---

### Task 8: Composition Root, Healthcheck Server, Dockerfile & CI/CD

**Files:**
- Create: `app/main.py`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `CI-CD.md`
- Modify: `.github/workflows/docker-multiarch.yml`
- Create: `tests/test_main.py`

**Interfaces:**
- Produces: executable application `python -m app.main`, Docker container with healthcheck on port 8080.

- [ ] **Step 1: Write failing test for healthcheck HTTP server**
Test that `GET /actuator/health` and `GET /health` return HTTP 200 `{"status": "UP"}`.

- [ ] **Step 2: Implement `app/main.py`**
Implement Composition Root: initialize async database, execute `FaqInitializer`, setup `McpRouter`, instantiate active `LlmClient`, configure `aiogram` Dispatcher, launch background HTTP healthcheck server on port 8080, and handle SIGINT/SIGTERM gracefully.

- [ ] **Step 3: Update `Dockerfile` and `docker-compose.yml`**
Write multi-stage Dockerfile with `uv`, non-root user `bot`, and healthcheck.

- [ ] **Step 4: Update CI/CD workflow `.github/workflows/docker-multiarch.yml` and `CI-CD.md`**
Update test step to `pytest` and Docker build step to use the new Dockerfile.

- [ ] **Step 5: Run full test suite and verify 100% PASS**
```bash
pytest -v
```

- [ ] **Step 6: Commit**
```bash
git add app/main.py Dockerfile docker-compose.yml CI-CD.md .github/ tests/test_main.py
git commit -m "feat: complete Python migration with healthcheck, Dockerfile, and CI/CD configuration"
```
