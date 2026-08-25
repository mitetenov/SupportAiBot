# MCP SDK v2 Migration Design

## Цель

Перевести `mcp-remnawave`, `bedolaga-mcp` и MCP-клиент `supportBot` на последние
стабильные SDK v2, сохранив существующие инструменты, ограничения support-режима,
изоляцию двух MCP-серверов и возможность поэтапного развёртывания без одновременного
обновления всех трёх компонентов.

## Зафиксированные версии

На 2026-08-25 последняя стабильная версия TypeScript MCP SDK — `2.0.0`,
опубликованная разделёнными пакетами. Для `mcp-remnawave` нужны
`@modelcontextprotocol/server@2.0.0` и `@modelcontextprotocol/node@2.0.0`; монолитный
`@modelcontextprotocol/sdk` v1 должен исчезнуть из manifest, lock-файла и imports.

Последняя стабильная версия Python MCP SDK — `mcp==2.0.0`. Её используют и
`bedolaga-mcp`, и `supportBot`; `supportBot` подключает SDK как клиент, а не продолжает
поддерживать собственную реализацию JSON-RPC/SSE/handshake.

SDK v2 реализуют современную ревизию протокола `2026-07-28` и совместимость с
handshake-ревизиями до `2025-11-25` включительно.

## Базовые ветки

Перед разработкой исполнитель обязан выполнить `git fetch --prune origin` в каждом
репозитории и создать отдельную ветку `feat/mcp-sdk-v2` непосредственно от свежего
remote base:

- `/Users/mikhail/supportBot`: `origin/master`;
- `/Users/mikhail/mcp-remnawave`: `origin/main`;
- `/Users/mikhail/bedolaga-mcp`: `origin/main`.

На момент подготовки документа remote refs были:

- `supportBot`: `22484764b02a92c0a9c0c424fed6a1fc9a3482f0`;
- `mcp-remnawave`: `513a29be752c19d90dc4a96b038bbe905acdc6e7`;
- `bedolaga-mcp`: `af596ffa810f9b68ee68ccd16593b2262f767c9a`.

Эти SHA — снимок, а не замена повторному fetch. Локальный коммит
`mcp-remnawave/feat/subscription-url-by-telegram` и локальный ahead-коммит
`bedolaga-mcp/main` не должны попасть в новые ветки автоматически.

## Архитектура совместимости

### mcp-remnawave

TypeScript-сервер использует один `createServer(config)` и один реестр инструментов
для обеих protocol eras:

- modern `2026-07-28`: `createMcpHandler(..., { legacy: "reject" })`, stateless
  request-per-server, без `Mcp-Session-Id`;
- legacy `2024-11-05` … `2025-11-25`: существующий sessionful путь, перенесённый на
  `NodeStreamableHTTPServerTransport`, с независимыми сессиями и `DELETE`.

`isLegacyRequest()` маршрутизирует запрос до одного из двух путей. Это сохраняет
совместимость со старым `supportBot`, который требует session ID, и одновременно
включает современный v2 wire protocol.

### bedolaga-mcp

Python-сервер переходит с `FastMCP` на `MCPServer`. Один
`streamable_http_app()` SDK v2 автоматически обслуживает modern и legacy eras.
`stateless_http=False` сохраняет sessionful legacy-поведение, а modern-запросы всё
равно остаются бессессионными. HTTP endpoint остаётся `/`, health — `/health`, stdio
использует тот же server factory и тот же реестр восьми read-only инструментов.

### supportBot

`HttpMcpClient` сохраняет узкий внутренний контракт с `McpRouter`, но внутри использует
официальный `mcp.client.Client` v2 в режиме `auto`:

- сначала `server/discover` и modern `2026-07-28`;
- автоматический fallback на legacy `initialize` для старых серверов;
- SDK отвечает за wire encoding, SSE/JSON, protocol headers и завершение legacy
  session;
- wrapper отвечает за cached tool descriptors, независимость двух серверов,
  безопасное представление `CallToolResult`, admin alerts и единичный reconnect при
  потере legacy session.

`McpRouter`, owner-based allowlists, collision hiding, readonly-политика Remnawave и
пиннинг Telegram/Bedolaga identity не меняются.

## Контракт результата инструмента в supportBot

Адаптер возвращает LLM строку и не передаёт наружу Python model repr:

1. `is_error=True` превращается в JSON `{"error": "<safe text>"}` и вызывает admin
   notification; такой результат не считается причиной reconnect.
2. Непустой `structured_content` сериализуется как JSON с `ensure_ascii=False`.
3. Один успешный text block возвращается как его text; это сохраняет JSON-текст,
   который уже возвращают оба MCP-сервера.
4. Несколько или нетекстовые content blocks сериализуются в JSON-массив wire-полей
   через Pydantic `model_dump(mode="json", by_alias=True, exclude_none=True)`.

## Версионирование и rollout

- `mcp-remnawave`: `3.2.1` → `3.3.0`;
- `bedolaga-mcp`: `1.1.0` → `1.2.0`;
- `supportBot`: поле version в `pyproject.toml` не повышается вручную, потому что его
  CI сам выбирает следующий minor в рамках major; меняются dependency lock и default
  image tags.

Сначала публикуются оба MCP-сервера, затем обновляется `supportBot`. Новый server v2
обслуживает старый клиент через legacy path; новый client v2 умеет откатиться на
старый server v1 через initialize. Поэтому каждый компонент можно откатывать отдельно.

## Вне scope

- Изменение бизнес-логики инструментов и их имён.
- Добавление новых MCP tools/resources/prompts.
- Redis/БД для MCP sessions или запуск нескольких legacy-session workers.
- OAuth, публичная публикация внутренних MCP endpoints и изменение API-токенов.
- Использование новых v2 extensions, subscriptions, sampling или input-required.
- Изменение LLM routing, prompt и identity security policy.

## Требования к покрытию агентом

Этот design и связанный implementation plan не содержат тестового исходного кода.
При реализации агент обязан добавить или обновить покрытие, перечисленное в каждом
task плана, и прогнать существующие проверки. Нельзя удалить regression-сценарий,
чтобы получить зелёный pipeline, или снизить coverage threshold.

## Источники документации

Документация получена через Context7 из официальных репозиториев:

- TypeScript v2 migration:
  <https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/migration/upgrade-to-v2.md>
- TypeScript HTTP and dual-era serving:
  <https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/serving/http.md>
- TypeScript protocol eras:
  <https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/protocol-versions.md>
- TypeScript legacy-routing example:
  <https://github.com/modelcontextprotocol/typescript-sdk/blob/main/examples/legacy-routing/server.ts>
- Python v2 changes:
  <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md>
- Python v2 client:
  <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md>
- Python client transports:
  <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md>
- Python legacy clients:
  <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/legacy-clients.md>

