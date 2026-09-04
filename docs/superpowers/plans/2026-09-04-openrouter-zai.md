# OpenRouter и Z.AI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить OpenRouter и Z.AI как основные и резервные LLM-провайдеры supportBot с текстовыми ответами, MCP tool calling, reasoning, существующим RAG, учётом токенов и диагностикой.

**Architecture:** Два отдельных адаптера `OpenRouterClient` и `ZaiClient` используют небольшой общий `ChatCompletionsClient` поверх существующего `AbstractLlmClient`. Общий слой отвечает за формат Chat Completions и HTTP, адаптеры — за настройки, reasoning и ошибки конкретного сервиса; фабрика подключает их к существующему `LlmFallbackClient`. Существующие OpenAI, Gemini, DeepSeek и Groq не переводятся на новую базу в рамках этой работы.

**Tech Stack:** Python `>=3.14`, Pydantic Settings, существующий `httpx`, pytest/pytest-asyncio, `httpx.MockTransport`, uv, Ruff, mypy.

**Spec:** Запрос пользователя: «напиши план интеграции еще двух провайдеров — openrouter и z.ai … не пиши код, а только опиши что нужно сделать и что проверить агенту». Самодостаточный контракт реализации находится ниже, в разделе «Контракт интеграции» этого файла. Отдельного утверждённого design-документа для этой функции нет; выбранные здесь границы и настройки являются проектными решениями плана.

## Global Constraints

- Этот документ содержит только инструкции; реализации и исходного кода тестов здесь намеренно нет по требованию пользователя.
- Изменения реализации ограничены репозиторием supportBot; соседние mcp-remnawave и bedolaga-mcp не затрагивать.
- Сохранить Python `>=3.14`, существующие зависимости и coverage gate `85%` из `pyproject.toml`.
- Не добавлять OpenAI SDK, SDK Z.AI, OpenRouter SDK или фреймворк маршрутизации: достаточно установленного `httpx`.
- Сохранить текущие defaults `LLM_PROVIDER=openai`, `EMBEDDING_PROVIDER=gemini`, `REASONING_EFFORT=none`.
- Не изменять prompt, правила эскалации, MCP allowlist, семантику RAG, хранение истории и политику повторного выполнения инструментов.
- Сохранить `BOT_LOG_LEVEL`: `TRACE`, `INFO`, `ERROR`; секреты скрывать на каждом уровне, полные диагностические payloads оставлять только в TRACE.
- Автоматические проверки выполнять offline, с фиктивными ключами и MockTransport. Не запускать настоящий бот или внешнюю генерацию ради unit/integration-тестов.
- Существующий opt-in `--confirm-external-api` у live eval сохраняется; этот план сам по себе не разрешает платные запросы или production rollout.
- Не менять настоящий `.env`, не публиковать изменения и не включать посторонние изменения в коммиты.

---

## Контекст репозитория

План подготовлен 2026-09-04 по checkout `27c987b`. Перед реализацией сопоставить названия и точки расширения с актуальным checkout: номера строк могут измениться. Во время подготовки уже существовал посторонний untracked-файл `docs/superpowers/plans/2026-08-27-ticket-media-to-operator-topic.md`; не включать его в свои изменения.

Прочитать перед первым изменением:

| Файл и точка входа | Что важно для реализации |
|---|---|
| `app/config.py`: `Settings`, `_parse_fallback_chain`, `_validate_llm_target`, `llm_provider_targets` | Валидация ключей и моделей; fallback разбирается по **первому** двоеточию через `partition`, поэтому внутри model допустимы дополнительные двоеточия |
| `app/llm/__init__.py`: `create_llm_client`, `_create_provider_client` | На каждую цель создаётся копия Settings с её provider/model; несколько целей оборачиваются в координатор |
| `app/llm/base.py`: `LlmResponse`, `AbstractLlmClient.do_chat`, `run_tool_calls`, `save_usage` | Общий цикл MCP, лимит итераций, повторные подсказки, история и token usage уже реализованы |
| `app/llm/fallback.py`: `is_fallback_eligible`, `_respond`, `_persist_success` | FAQ подготавливается один раз; завершённые действия кэшируются в пределах обращения; история сохраняется после успеха |
| `app/llm/deepseek.py`, `app/llm/groq.py` | Образцы Chat Completions, формы tool calls и владения HTTP-клиентом; их модельные параметры нельзя копировать автоматически |
| `app/llm/openai_client.py` | Использует Responses API; новый OpenRouter-клиент нельзя получить простой заменой его base URL |
| `app/retry.py`: `post_with_retry` | Единая retry-политика, максимум три попытки по умолчанию, обработка Retry-After |
| `app/main.py`: создание общего `httpx.AsyncClient` | У общего клиента timeout 30 секунд; локальный timeout нового адаптера должен явно передаваться в вызов HTTP |
| `app/logging_http.py`, `app/logging_config.py`, `app/logging_redaction.py` | HTTP hooks, TRACE, безопасные ошибки и автоматическая регистрация credential-полей Settings |
| `tests/conftest.py` | Тесты отключают загрузку `.env`, очищают env-префиксы и ускоряют retry backoff |
| `benchmarks/agent_behavior_eval.py`: `run_once` | Eval уже использует production-фабрику и принимает внедрённый HTTP-клиент |
| `.github/workflows/docker-multiarch.yml`, `pyproject.toml` | Точные обязательные команды проверки |

Особенность текущей схемы БД: `LlmTokenUsage` хранит количества токенов, но не provider/model. В этой задаче сохраняется именно существующая схема; не обещать разбивку стоимости по провайдерам и не добавлять миграцию.

## Контракт интеграции

### 1. Границы функциональности

Оба новых провайдера должны работать через обычный `chat`, в Telegram, Bedolaga support и операторском `/ask` без отдельных provider-веток в этих сценариях. В состав входят история, FAQ-контекст, последовательные и несколько одновременно запрошенных MCP-инструментов, повторный запрос после tool results, reasoning и fallback.

В этой версии новые адаптеры поддерживают **текст и инструменты**: `supports_images()` возвращает `False`. Это ограничение реализации, а не утверждение о возможностях всех моделей OpenRouter или Z.AI. При изображении существующий fallback должен пропустить эти адаптеры и выбрать настроенный OpenAI/Gemini; без подходящей цели вернуть существующую понятную ошибку. Нельзя молча отбросить картинку и ответить только на подпись. Multimodal, streaming, embeddings новых провайдеров, динамический каталог моделей и управление внутренним routing OpenRouter не входят в этот план.

Идентификаторы провайдеров: `openrouter` и `zai`. `z.ai` — название сервиса, но не дополнительный alias в `LLM_PROVIDER`. Отображаемые имена: `OpenRouter` и `Z.AI`.

### 2. Настройки

| ENV / атрибут Settings | Тип / default | Требование |
|---|---|---|
| `OPENROUTER_API_KEY` / `openrouter_api_key` | `SecretStr | None`, `None` | Непустой ключ обязателен только для активной основной или резервной цели OpenRouter |
| `OPENROUTER_MODEL` / `openrouter_model` | `str | None`, `None` | Обязателен для основной цели; пример `z-ai/glm-4.7`; модель резервной цели берётся из цепочки |
| `OPENROUTER_BASE_URL` / `openrouter_base_url` | `str`, `https://openrouter.ai/api/v1` | Базовый адрес без `/chat/completions`; завершающий slash допустим |
| `OPENROUTER_TIMEOUT_SECONDS` / `openrouter_timeout_seconds` | `float`, `120.0` | Строго положительное конечное число; применяется и при внедрённом HTTP-клиенте |
| `ZAI_API_KEY` / `zai_api_key` | `SecretStr | None`, `None` | Непустой ключ обязателен только для активной основной или резервной цели Z.AI |
| `ZAI_MODEL` / `zai_model` | `str | None`, `None` | Обязателен для основной цели; пример `glm-4.7`; модель резервной цели берётся из цепочки |
| `ZAI_BASE_URL` / `zai_base_url` | `str`, `https://api.z.ai/api/paas/v4` | General API; завершающий slash допустим |
| `ZAI_TIMEOUT_SECONDS` / `zai_timeout_seconds` | `float`, `120.0` | Строго положительное конечное число; применяется и при внедрённом HTTP-клиенте |

Не задавать изменчивую «последнюю модель» default-значением. Не применять проверку префикса OpenAI `sk-` к ключам этих сервисов. Для новых активных целей отклонять пустые/пробельные ключ, model и base URL; base URL должен иметь HTTP(S) scheme и hostname. Не допускать userinfo, query, fragment или уже добавленный `/chat/completions`, чтобы не получать неоднозначный адрес и credential-bearing URL. HTTP разрешён для локального mock/proxy; default остаётся HTTPS. Ошибка должна называть ENV-поле, а не показывать его значение.

Дополнить `VALID_LLM_PROVIDERS`, но сохранить `VALID_EMBEDDING_PROVIDERS` как `gemini`, `openai`. Новый ключ не заменяет отдельный ключ embedding-провайдера. Тестовые фикстуры должны очищать `OPENROUTER_` и `ZAI_`; заодно добавить отсутствующий сейчас `GROQ_`, чтобы матрица совместимости не зависела от окружения разработчика.

Модель считается непрозрачной строкой: не обрезать namespace, slash, внутренние двоеточия и суффиксы. Тест парсера: `openrouter:vendor/model:variant,zai:glm-4.7` даёт две цели; model первой — ровно `vendor/model:variant`. Это синтетическая строка для парсера, не утверждение о существовании модели. Порядок и повторы одного провайдера с разными моделями сохраняются.

Базовые адреса подтверждены официальными руководствами [OpenRouter Quickstart](https://openrouter.ai/docs/quickstart) и [Z.AI OpenAI-compatible API](https://docs.z.ai/guides/develop/openai/python). Coding Plan использует отдельный режим доступа: не переключать endpoint автоматически по ошибке ключа или названию модели. Возможность конкретной пары key/model/endpoint проверяется перед реальным включением; документация Z.AI содержит различающиеся endpoint-примеры для отдельных аккаунтов и моделей.

### 3. Формат запросов, ответов и MCP

- POST на нормализованный base URL с единственным `/chat/completions`; Bearer-ключ и JSON Content-Type выставляются на каждом запросе. Не изменять default headers общего клиента: иначе ключи могут попасть к другому сервису.
- Передавать `model`, `messages`, `stream=false`; `tools` и `tool_choice=auto` включать только при непустом списке инструментов. Параметры sampling, `max_tokens`, `parallel_tool_calls`, server tools и OpenAI Responses-поля автоматически не добавлять.
- Сохранять порядок: основной system prompt, динамический system context с FAQ/user ID, история, новый user message. Не мутировать входную историю или схему MCP.
- Tool definitions брать только из `McpRouter.list_tools()`, с существующими именами, description и input schema. Пустая schema получает существующий объектный default.
- Разбирать `choices[0].message`: строковый `content`, либо `null` для tool-only ответа; tool call содержит непустые `id`, `function.name` и объект аргументов. Принимать строку JSON-объекта или уже разобранный объект. Невалидный JSON, массив/scalar вместо аргументов, отсутствующие обязательные поля и дублирующиеся call IDs в одном ответе завершают разбор `LlmProcessingException` **до выполнения любого инструмента**. Не копировать существующее превращение сломанного JSON в пустой объект.
- Ответ assistant с tool calls добавлять перед всеми tool results; каждый результат содержит исходный `tool_call_id`. Сериализация аргументов должна сохранять их значения, включая Unicode и вложенные объекты.
- Reasoning остаётся отдельными полями. Финальный ответ, история, Telegram и Bedolaga получают только пользовательский `content`; завершённые и незавершённые служебные `<think>`-блоки из content удаляются по существующему принципу Groq. Reasoning-only ответ без текста и tools приводит к существующей ошибке отсутствия ответа.
- `usage.prompt_tokens`, `completion_tokens`, `total_tokens` переводить в `TokenUsage`. Без usage возвращать `None`, без отдельных счётчиков использовать нули; отсутствие `total_tokens` допускает сумму input/output. Не прибавлять reasoning/cache tokens повторно к completion/prompt. Отрицательные или нечисловые счётчики считать отсутствующей статистикой, не причиной потери валидного ответа.

### 4. Reasoning: явная матрица поддержки

Не переносить настройки прямого OpenAI/DeepSeek на сервис-посредник по совпадению имени модели. Начальный список проверяемых профилей небольшой и находится в соответствующих адаптерах, а не в глобальном каталоге. Model ID, отправляемый API, остаётся исходным; сравнение с известным профилем может быть нормализовано отдельно.

| Клиент / точная модель | `none` | Остальные профили | Что уходит в запрос |
|---|---|---|---|
| OpenRouter / `z-ai/glm-4.7` | выключено | `minimal`–`max` → включено без обещания градаций | `reasoning.enabled`; не отправлять native `thinking` |
| OpenRouter / `z-ai/glm-5.3` | ошибка конфигурации до HTTP | `minimal`, `low` → `low`; `medium`, `high` → `high`; `xhigh`, `max` → `max` | `reasoning.effort` с выбранным значением |
| Z.AI / `glm-4.7` | выключено | `minimal`–`max` → включено без обещания градаций | `thinking.type`: `disabled` / `enabled`; без `reasoning_effort` |
| Z.AI / `glm-5.3` | ошибка конфигурации до HTTP | `minimal`, `low` → `low`; `medium`, `high` → `high`; `xhigh`, `max` → `max` | `thinking.type=enabled`, native `reasoning_effort` |
| Любая другая модель, включая неподтверждённые aliases/variants | модельный default | модельный default | Не добавлять неизвестные reasoning-параметры; INFO явно сообщает `unsupported/ignored` |

Это клиентская политика преобразования общего профиля, а не обещание одинакового расхода токенов разных моделей. Неизвестные модели допускаются для текста и tools; их reasoning не гарантируется и настройка `none` не обещает его отключения. Возможности инструментов выбранной неизвестной модели проверяет оператор по каталогу перед использованием. Список поддерживаемых reasoning-профилей расширяется только вместе с источником и тестами; не использовать догадки вида «все glm-* одинаковые».

В текущем [каталоге OpenRouter](https://openrouter.ai/api/v1/models) GLM-4.7 имеет переключаемое reasoning, а GLM-5.3 — обязательное с уровнями low/high/max. Единый объект `reasoning` и необходимость сохранять `reasoning_details` описаны в [OpenRouter Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens). Для прямого API различие подтверждают [Z.AI Thinking Mode](https://docs.z.ai/guides/capabilities/thinking-mode) и [GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3). Перед реализацией сверить эти страницы; изменения API отразить в таблице и тестах, не подменять модель молча.

OpenRouter: не ставить `reasoning.exclude=true`: ответные reasoning-блоки могут быть нужны для следующего шага MCP. Сохранять `message.reasoning` и структурированный `message.reasoning_details` в исходном порядке и с неизвестными внутренними полями. Для typed-состояния добавить локальный `OpenRouterResponse`, наследующий `LlmResponse`, с полем `reasoning_details: list[dict[str, Any]]` и отдельным списком по умолчанию для каждого экземпляра. Поле `LlmResponse.reasoning_content` использовать для строкового `message.reasoning`. При продолжении восстанавливать именно ключи OpenRouter, а не `reasoning_content`.

Z.AI: возвращаемый `message.reasoning_content` сохранять в существующем `LlmResponse.reasoning_content` и возвращать без изменений в последующие assistant-сообщения текущего tool loop. Не включать дополнительный режим `clear_thinking=false`: межобращенческое хранение thinking не входит в задачу. Reasoning любого провайдера не должен попадать к другой fallback-цели: существующий перенос результатов MCP остаётся provider-neutral.

`get_effective_reasoning_effort()` должен возвращать фактический режим: `none`, `enabled`, `low`, `high`, `max` или `unsupported/ignored`. При запрете `none` ошибка должна безопасно указать provider/model и предложить `low`, не выводя Settings. Проверить это и для модели, указанной только в fallback chain.

### 5. Ошибки, повторы и timeout

Сохранить текущую таблицу fallback: HTTP `401`, `402`, `403`, `408`, `413`, `429`, `500`, `502`, `503`, `504`, timeout/transport и распознанное исчерпание баланса. Для HTTP-повторов использовать только `post_with_retry`; не добавлять второй цикл retry в адаптере. `400`, `404`, `422`, malformed JSON, неправильный payload, невалидные tools и неизвестный код ошибки не становятся автоматически причиной fallback.

Оба клиента должны отвергать top-level `error` даже при HTTP 200. Для OpenRouter документированный числовой `error.code`, совпадающий с HTTP-кодом, можно нормализовать в `LlmProcessingException.status_code`; строковое представление того же числа допустимо. Неизвестное значение не интерпретировать как временный сбой. Для Z.AI бизнес-код не является HTTP status: например, `1113` — исчерпание баланса, `1210` — неверные параметры. `1113` даёт `fallback_eligible=True`, `1210` внутри HTTP 200 не даёт fallback; остальные body-коды без фактического ошибочного HTTP статуса считать обычной безопасной ошибкой до добавления отдельного подтверждённого правила. При настоящем HTTP >=400 сохранять фактический HTTP status. Источник соответствий: [Z.AI Errors](https://docs.z.ai/api-reference/api-code).

Body-error при HTTP 200 не повторять скрытым циклом: либо разрешённый переход к следующей цели, либо доменная ошибка. Не переносить сырой provider error message в публичное исключение. Безопасные метаданные: provider, model, status, нормализованный код и тип сбоя. Сырые ответы доступны в TRACE через существующую очистку секретов.

Timeout 120 секунд — timeout HTTP-операций, а не гарантированный общий SLA обращения: несколько retry, tool iterations и fallback-целей увеличивают суммарное время. Не менять timeout Telegram, MCP или embeddings через общий HTTP-клиент.

## Карта файлов

| Действие | Файл | Ответственность |
|---|---|---|
| Создать | `app/llm/chat_completions.py` | Общие текстовые messages/tools, HTTP lifecycle, strict parsing, token usage |
| Создать | `app/llm/openrouter.py` | `OpenRouterClient`, `OpenRouterResponse`, параметры и состояние OpenRouter |
| Создать | `app/llm/zai.py` | `ZaiClient`, параметры и бизнес-ошибки Z.AI |
| Изменить | `app/config.py` | Новые поля и проверка активных целей |
| Изменить | `app/llm/__init__.py` | Импорты, exports и две ветки фабрики |
| Изменить | `tests/conftest.py` | Изоляция новых env-префиксов |
| Создать | `tests/test_chat_completions_client.py` | Общий контракт wire-format, ошибок и HTTP lifecycle |
| Создать | `tests/test_openrouter_client.py` | Запросы, parsing, reasoning и ошибки OpenRouter |
| Создать | `tests/test_zai_client.py` | Запросы, parsing, reasoning и ошибки Z.AI |
| Изменить | `tests/test_config.py`, `tests/test_main.py` | Настройки и production-фабрика |
| Изменить | `tests/test_llm_fallback.py` | Переключение, единственность side effects и image filtering |
| Изменить | `tests/test_agent_behavior_eval.py` | Offline eval через реальные новые адаптеры |
| Создать | `tests/test_new_provider_logging.py` | Логи двух новых клиентов и credential redaction |
| Изменить | `.env.example`, `README.md` | Настройка, возможности, ограничения и порядок проверки |

Только при доказанном дефекте менять `app/llm/base.py`, `app/llm/fallback.py`, `app/logging_redaction.py` или `benchmarks/agent_behavior_eval.py`: сначала отдельный падающий regression-тест, затем минимальное исправление. Само добавление двух адаптеров не требует изменений этих файлов. `app/main.py`, БД, `pyproject.toml`, `uv.lock`, Docker Compose и CI не должны требовать функциональных изменений: compose уже передаёт `.env`.

## Общий порядок работы агента

- [ ] Прочитать применимые `AGENTS.md`, этот документ и файлы из таблицы контекста; проверить текущий git status и сохранить чужие изменения.
- [ ] Зафиксировать исходный результат `uv lock --check`; подготовить зависимости через `uv sync --frozen --extra dev --no-install-project`, если среда ещё не готова.
- [ ] Запустить исходные `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, `uv run --no-sync mypy` и `uv run --no-sync pytest -v`; отличать исходные проблемы от новых.
- [ ] Выполнять задачи по порядку. В каждой сначала добавить описанные тестовые сценарии, убедиться в целевом падении, затем реализовать минимальное изменение и получить PASS. Ошибка отсутствия зависимости не считается ожидаемым RED.
- [ ] Для быстрых локальных выборок применять `pytest --no-cov`: глобальный coverage floor нельзя оценивать по маленькой выборке. Финальный полный прогон выполняется без `--no-cov`.
- [ ] После каждой задачи просмотреть её diff и создать отдельный локальный commit только с перечисленными файлами. Не применять `git add .`; имена коммитов ниже — рекомендуемые.

## Task 1: Общий текстовый Chat Completions клиент

**Files:** Create `app/llm/chat_completions.py`, `tests/test_chat_completions_client.py`.

**Interfaces:**
- Consumes: существующие `AbstractLlmClient`, `LlmResponse`, `ToolCall`, `TokenUsage`, `McpRouter`, `ChatHistoryService`, `FaqEmbeddingService`, `DatabaseSessionManager`, `post_with_retry`.
- Produces: `ChatCompletionsClient(AbstractLlmClient)` с конструктором `__init__(mcp_router: McpRouter, chat_history_service: ChatHistoryService, faq_embedding_service: FaqEmbeddingService, db_manager: DatabaseSessionManager | None = None, http_client: httpx.AsyncClient | None = None, *, model: str, base_url: str, api_key: str, request_timeout_seconds: float = 120.0) -> None`.
- Общие методы: `build_request_body(messages: list[dict[str, Any]]) -> dict[str, Any]`, `parse_response(payload: dict[str, Any]) -> LlmResponse`, `extract_usage(payload: dict[str, Any]) -> TokenUsage | None`; `build_initial_conversation`, `call_api`, `add_tool_calls_to_conversation`, `add_tool_result_to_conversation` сохраняют точные сигнатуры `AbstractLlmClient`.
- HTTP extensions: `check_response_error(response: httpx.Response, payload: dict[str, Any] | None) -> None` обрабатывает HTTP >=400 и body-error; адаптеры могут уточнить классификацию. `http_client` — lazy property, `close() -> None` — async. Provider name задаёт конкретный адаптер через существующий `get_provider_name()`.

- [ ] Добавить тестовую минимальную конкретную реализацию общего класса внутри тестового файла; она имеет фиксированное имя провайдера, синтетическую модель и не требует расширения `Settings`.
- [ ] Описать тесты request/messages: два system-сообщения, FAQ и user ID, исходная история без мутации, Unicode, `stream=false`, отсутствие tools при пустом router и сохранение schema при непустом.
- [ ] Описать тесты parsing: обычный текст, tool-only content=null, два tools, вложенные аргументы, аргументы строкой и объектом, неправильный JSON аргументов, scalar/list, пустые ID/имя, duplicate IDs, отсутствие choices/message и неправильный тип content. Для каждого отрицательного случая ожидается `LlmProcessingException`, а MCP не вызывается.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_chat_completions_client.py -q`; убедиться, что тесты падают из-за отсутствующего общего клиента/поведения.
- [ ] Реализовать сообщения, определения tools, строгий разбор, сборку tool messages и обработку `<think>` согласно контракту. `supports_images()` должен быть `False`; прямой `chat_with_image` завершается до HTTP.
- [ ] Добавить MockTransport-тесты полного URL, Bearer header, JSON, timeout override при внедрённом клиенте, HTTP 401/429/500, HTTP 200 с error, invalid JSON и JSON-массива вместо объекта. До декодирования ошибочного HTTP-body сохранить статус: HTML-ошибка 503 должна оставаться 503.
- [ ] Реализовать HTTP через `post_with_retry`, явный `timeout=request_timeout_seconds` и безопасную классификацию ошибок. По возможности декодировать error JSON один раз; не превращать malformed body HTTP >=400 в потерю исходного статуса. Не логировать сырой текст на INFO/ERROR.
- [ ] Добавить тесты ownership: свой клиент создаётся лениво с logging hooks и закрывается; переданный клиент после `close()` остаётся открыт; два адаптера на одном HTTP-клиенте не меняют его default headers или timeout. `CancelledError` не превращается в обычную ошибку/fallback.
- [ ] Добавить проверки usage: полный набор счётчиков, без usage, частичные поля, отдельные reasoning/cache counters, отрицательные/нечисловые значения. Проверить, что некорректная статистика не мешает вернуть нормальный ответ.
- [ ] Реализовать usage и lifecycle; повторить `uv run --no-sync pytest --no-cov tests/test_chat_completions_client.py tests/test_abstract_llm_client.py tests/test_retry.py -q`. Ожидается PASS.
- [ ] Проверить diff и сделать commit `feat: add shared chat completions transport`.

**Результат задачи:** автономно протестированный общий wire-format и HTTP-слой, пока не меняющий доступные production-провайдеры.

## Task 2: OpenRouter как полноценная цель фабрики

**Files:** Create `app/llm/openrouter.py`, `tests/test_openrouter_client.py`; Modify `app/config.py`, `app/llm/__init__.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_main.py`, `.env.example`, `README.md`.

**Interfaces:**
- Consumes: `ChatCompletionsClient` из Task 1 и `Settings.llm_provider_targets`.
- Produces: `OpenRouterClient(ChatCompletionsClient)` с тем же публичным конструктором, что `DeepSeekClient`: `settings`, `mcp_router`, `chat_history_service`, `faq_embedding_service`, необязательные `db_manager`, `http_client`, с существующими типами.
- Produces: `OpenRouterResponse(LlmResponse)` с полем `reasoning_details`; `parse_response(payload: dict[str, Any]) -> OpenRouterResponse`; `build_request_body`, `add_tool_calls_to_conversation`, `check_response_error`, `get_effective_reasoning_effort` уточняют общий контракт. Фабричная сигнатура не меняется.

- [ ] Добавить config-тесты: primary `openrouter`, нормализация регистра provider, четыре новых поля/defaults, пробельный ключ/модель/URL, invalid URL, timeout 0/отрицательный/NaN/Infinity, отсутствие ключа активной fallback-цели и отсутствие необходимости в ключе неактивного провайдера.
- [ ] Добавить config-тесты модели с namespace и внутренним двоеточием, fallback-only OpenRouter без `OPENROUTER_MODEL`, независимого embedding key и запрета `EMBEDDING_PROVIDER=openrouter`. Добавить очистку `OPENROUTER_` и `GROQ_` в fixtures.
- [ ] Добавить factory-тест: primary OpenRouter создаётся без fallback-обёртки; две OpenRouter-цели с разными моделями создаются раздельно, сохраняют общий HTTP-клиент и не мутируют исходный Settings.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_config.py tests/test_main.py tests/test_openrouter_client.py -q`, получить целевой RED.
- [ ] Добавить поля Settings, валидацию активного target и ветку/exports фабрики. В адаптере раскрывать только `openrouter_api_key` через `reveal`, передавать общий класс и полный model ID. Display name — `OpenRouter`.
- [ ] Добавить тесты всех семи общих efforts для обеих известных моделей и неизвестной модели, каждый с tools и без tools. Проверить JSON-параметры и effective effort по таблице; `glm-5.3` + `none` отвергается при создании primary и fallback клиента до HTTP.
- [ ] Реализовать только профили из таблицы; не включать `reasoning.exclude`, не отправлять DeepSeek `thinking`, OpenAI `input`/`instructions` или произвольные attribution headers. Не вводить runtime-запрос к каталогу моделей.
- [ ] Добавить тест двух последовательных MCP-итераций: разные tool IDs, строковый reasoning, несколько `reasoning_details`, в том числе элемент с encrypted data/signature и неизвестным полем. Следующие assistant messages сохраняют блоки структурно без изменений и в том же порядке; финальный text/history их не содержат.
- [ ] Реализовать `OpenRouterResponse`, разбор и восстановление reasoning-полей только в адаптере. Общий parser сначала валидирует все tool calls; ошибка в последнем tool не должна оставлять выполненным первый.
- [ ] Добавить HTTP-тесты обычного ответа и body-error при 200 с code 402, строкой `429`, 400 и неизвестным значением. Подтвердить классификацию через `is_fallback_eligible`, отсутствие двойного retry и отсутствие body/key в `str(exception)`/пользовательском тексте.
- [ ] Дополнить `.env.example` и README четырьмя переменными OpenRouter, примером `z-ai/glm-4.7`, правилом fallback-only модели и текущим ограничением на изображения. Ключ в примере заведомо фиктивный; default основного провайдера не менять.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_openrouter_client.py tests/test_chat_completions_client.py tests/test_config.py tests/test_main.py -q`; проверить PASS, затем commit `feat: integrate OpenRouter LLM provider`.

**Результат задачи:** OpenRouter выбирается через `.env` и фабрику, выполняет текстовый MCP-диалог и имеет самостоятельную документацию и offline-тесты.

## Task 3: Z.AI как полноценная цель фабрики

**Files:** Create `app/llm/zai.py`, `tests/test_zai_client.py`; Modify `app/config.py`, `app/llm/__init__.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_main.py`, `.env.example`, `README.md`.

**Interfaces:**
- Consumes: `ChatCompletionsClient`, `Settings`, `LlmResponse.reasoning_content`, существующая фабрика.
- Produces: `ZaiClient(ChatCompletionsClient)` с публичным конструктором `settings`, `mcp_router`, `chat_history_service`, `faq_embedding_service`, необязательные `db_manager`, `http_client`, с типами существующих concrete clients.
- Методы `parse_response(payload: dict[str, Any]) -> LlmResponse`, `build_request_body`, `add_tool_calls_to_conversation`, `check_response_error`, `get_effective_reasoning_effort` сохраняют контракты общего класса; новые общие структуры данных не нужны.

- [ ] Добавить config/factory-тесты primary `zai`, нормализации provider, отсутствующих/пробельных ключа и модели, fallback-only модели, четырёх полей/defaults, URL и timeout, запрета embedding `zai` и независимости embedding credentials. `z.ai` должен давать стандартную ошибку неизвестного provider.
- [ ] Добавить env-префикс `ZAI_` в fixtures. Проверить, что экспортированная в тесте чужая переменная провайдера не влияет на соседние tests.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_zai_client.py tests/test_config.py tests/test_main.py -q` и подтвердить целевой RED.
- [ ] Реализовать Settings и фабрику для `zai`; display name — `Z.AI`. Конечный URL должен быть ровно `https://api.z.ai/api/paas/v4/chat/completions`, в том числе при base URL с завершающим slash. Ключ OpenAI/OpenRouter не может использоваться вместо ZAI key.
- [ ] Добавить parameterized-тесты семи efforts для `glm-4.7`, `glm-5.3` и неизвестной модели, с tools и без tools. Проверить mapping, отсутствие чужого объекта `reasoning`, запрет `glm-5.3` + `none` до HTTP и честное значение effective effort.
- [ ] Реализовать профили из таблицы. Для `glm-4.7` отправлять toggle, а не неподтверждённые уровни; для `glm-5.3` — native low/high/max. Не копировать правило DeepSeek, которое удаляет `tool_choice` при thinking.
- [ ] Добавить тест двух tool-итераций с `content=null`, непустым `reasoning_content` и несколькими инструментами. Все reasoning-блоки должны вернуться в assistant history текущего запроса без изменения; результаты связываются по исходным IDs; пользователь получает только финальный text.
- [ ] Реализовать сохранение `reasoning_content` в адаптере. Проверить отсутствие искусственного `clear_thinking=false`, отсутствие межобращенческого хранения и отсутствие OpenRouter `reasoning_details` в Z.AI request.
- [ ] Добавить error-тесты: HTTP 429 + business code `1113`, HTTP 400 + `1210`, HTML 503, HTTP 200 + `1113`, HTTP 200 + `1210`, неизвестный business code и transport timeout. Проверить фактический HTTP status, eligibility и отсутствие исходного error body в исключении.
- [ ] Реализовать классификацию бизнес-кодов; не записывать `1113` в поле HTTP status. Проверить, что 429 использует существующие три HTTP-попытки, а body-error 200 не создаёт дополнительный скрытый цикл.
- [ ] Дополнить `.env.example` и README четырьмя переменными Z.AI, примером `glm-4.7`, различием native model ID и OpenRouter namespace, General API endpoint и ограничением текст/tools. Не обещать совместимость Coding Plan ключа с обычным API.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_zai_client.py tests/test_chat_completions_client.py tests/test_config.py tests/test_main.py -q`; проверить PASS, затем commit `feat: integrate ZAI LLM provider`.

**Результат задачи:** Z.AI работает как самостоятельный provider через production-фабрику с корректным reasoning/tool loop.

## Task 4: Смешанный fallback и существующие сценарии поддержки

**Files:** Modify `tests/test_llm_fallback.py`, `tests/test_agent_behavior_eval.py`; при доказанном regression — минимальное изменение соответствующего адаптера или общего нового слоя. Существующие pipeline-файлы менять только при отдельном воспроизводимом дефекте.

**Interfaces:**
- Consumes: неизменённый `create_llm_client(...) -> LlmClient`, `LlmFallbackClient`, `LlmTurnState.completed_tool_results`, `run_once(cases: list[BehaviorCase] | None = None, settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> list[CaseResult]` из `benchmarks.agent_behavior_eval`.
- Produces: проверенные существующие внешние контракты; новых интерфейсов не добавлять.

- [ ] Дополнить real-client MockTransport-матрицу переходами OpenRouter → Z.AI, Z.AI → OpenRouter, DeepSeek → OpenRouter и Z.AI → Groq. Проверять адрес и Authorization каждой попытки, порядок моделей и возврат первой успешной цели.
- [ ] Добавить цепочку из трёх целей с двумя моделями OpenRouter. Первая цель завершается допустимой ошибкой, вторая — допустимой ошибкой после tool result, третья возвращает ответ. Ни model override, ни provider-specific reasoning не должны загрязнять соседний клиент.
- [ ] Для каждого нового primary проверить HTTP 401/402/403/408/413/429/500/502/503/504, TimeoutException и TransportError. Существующие параметризованные tests расширять там, где это сохраняет ясность. 413 делает одну HTTP-попытку; retryable status — три; затем переход, если есть цель.
- [ ] Добавить отрицательные тесты HTTP 400/404/422, invalid JSON, malformed tool arguments и исключения MCP с неизвестным исходом: следующий provider не вызывается, финальная история не сохраняется.
- [ ] Добавить сценарий завершённого изменяющего MCP tool: primary получил результат, затем API упал; backup запросил тот же tool с другими ID и порядком ключей JSON. Реальный router вызывается один раз, backup получает кэш; tool с новыми аргументами выполняется отдельно. В новом пользовательском обращении прежний кэш не действует.
- [ ] Добавить проверки RAG один раз, записи user/assistant один раз после успеха, отсутствия записи после полного провала и учёта токенов каждого успешного HTTP completion, включая tool-only ответы перед fallback.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_llm_fallback.py -q`. Если тест уже проходит на новой интеграции, сохранить его как regression guard; не менять production ради искусственного RED. Реальные падения исправлять минимально, сохраняя исходную retry/fallback-политику.
- [ ] Добавить image-тесты: новые клиенты не получают HTTP-запросов с картинкой; смешанная цепочка передаёт исходные image bytes/base64 и mime type OpenAI/Gemini; цепочка только из OpenRouter/Z.AI возвращает существующую ошибку до HTTP.
- [ ] Добавить offline eval по одному синтетическому случаю для каждого нового клиента через `run_once(..., settings=..., http_client=MockTransport-client)`: ожидаемый инструмент, синтетический result, финальный ответ и успешный `CaseResult`. Использовать существующие synthetic router/FAQ/history, не дублировать eval engine.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_llm_fallback.py tests/test_agent_behavior_eval.py tests/test_pipeline.py tests/test_bedolaga_pipeline.py tests/test_operator_ask.py -q`. Ожидается PASS без Telegram, БД и реальных API.
- [ ] Проверить diff и сделать commit `test: verify OpenRouter and ZAI fallback integration`; если потребовалось production-исправление, явно описать его в сообщении коммита.

**Результат задачи:** оба провайдера работают через существующие entrypoints, fallback не повторяет завершённые действия и не теряет контекст обращения.

## Task 5: Диагностика, документация и итоговая приёмка

**Files:** Create `tests/test_new_provider_logging.py`; Modify `README.md`, `.env.example`; только при падении теста — `app/llm/chat_completions.py`, `app/llm/openrouter.py`, `app/llm/zai.py`, `app/logging_redaction.py`.

**Interfaces:**
- Consumes: `setup_logging`, `register_settings_secrets`, `create_logging_hooks`, `get_effective_reasoning_effort`, оба новых production-клиента.
- Produces: прежний контракт логов и проверенная инструкция запуска; новых ENV-флагов логирования или API не добавлять.

- [ ] Добавить tests для TRACE/INFO/ERROR каждого провайдера. Использовать уникальные фиктивные ключи без известных префиксов и register_settings_secrets, чтобы проверять очистку по значению, а не только регулярку `sk-*`.
- [ ] В TRACE проверить финальный JSON request, tools, response, reasoning, номер retry и переход fallback; ключи в headers, вложенных body, error message и exception должны быть замаскированы. В INFO проверить только provider/model/configured/effective effort и безопасные события. В ERROR проверить безопасный тип/status/code без FAQ, переписки, tools arguments и provider body.
- [ ] Проверить отсутствие дорогой сериализации TRACE на INFO/ERROR, например подменой сериализатора, который вызывает ошибку при обращении. Убедиться, что logging redaction не изменяет реальные HTTP payloads и reasoning signatures.
- [ ] Проверить отдельные запросы с внедрённым клиентом, созданным с production logging hooks, и с собственным lazy-клиентом. Не навешивать hooks повторно на общий HTTP-клиент и не дублировать одинаковые transport-записи.
- [ ] Запустить `uv run --no-sync pytest --no-cov tests/test_new_provider_logging.py tests/test_logging_redaction.py tests/test_logging_transport.py tests/test_logging_integration.py -q`. При падении сначала локализовать новую границу логирования; затем исправить её и повторить тот же набор.
- [ ] Свести README: перечислить шесть провайдеров, все восемь новых ENV-полей, ограничения text/tools, отдельные embedding credentials, точную reasoning-матрицу и unknown-model поведение, retry/timeout semantics и отсутствие автоматической смены endpoint.
- [ ] Добавить три согласованных примера конфигурации: OpenRouter primary с `z-ai/glm-4.7`; Z.AI primary с `glm-4.7`; существующий OpenAI primary с цепочкой `openrouter:z-ai/glm-4.7,zai:glm-4.7`. Во всех примерах должны быть фиктивные ключи выбранных LLM и embeddings. Для `glm-5.3` отдельно показать требование выбрать ненулевой effort.
- [ ] Обновить описание eval, чтобы оно включало новые providers. Не добавлять автоматическую внешнюю проверку ключа при старте или в CI. Указать, что MockTransport подтверждает интеграцию, но не доступность конкретной модели для аккаунта.
- [ ] Сверить каждое имя ENV из README и `.env.example` с Settings; удостовериться, что обычный запуск без новых переменных сохраняет прежний выбор клиента. Проверить exports и отсутствие новых provider-веток в Telegram/Bedolaga handlers.
- [ ] Выполнить `uv lock --check`, затем `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, `uv run --no-sync mypy`, `uv run --no-sync pytest -v`. Все команды должны завершиться успешно; полный pytest обязан сохранить coverage не ниже 85%. Не менять gate и не перегенерировать lock без изменения зависимостей.
- [ ] Выполнить `git diff --check`; проверить итоговый diff на реальные ключи, правки `.env`, посторонние файлы, изменение defaults, зависимостей, CI или соседних репозиториев. Исправить только относящиеся к этой работе дефекты.
- [ ] Зафиксировать документацию и итоговые logging-проверки commit `docs: document OpenRouter and ZAI configuration`. В отчёте перечислить фактические результаты gates и явно отметить, запускалась ли внешняя проверка.

**Результат задачи:** реализация готова к review с воспроизводимыми offline-проверками и инструкцией настройки.

## Необязательная внешняя проверка после реализации

Она не является условием завершения написания кода и не запускается этим планом автоматически. Если пользователь отдельно разрешит внешние запросы, выполнить существующий eval с синтетическими данными, отдельно для каждого провайдера, с пустой fallback chain, чтобы успех резерва не скрывал сбой проверяемого клиента. Команда: `uv run --no-sync python -m benchmarks.agent_behavior_eval --runs 1 --threshold 0.8 --confirm-external-api`.

Перед таким запуском проверить доступность выбранной модели и endpoint в аккаунте, задать ключ через обычный защищённый канал конфигурации и не печатать его. После запуска записать provider, model, endpoint без credentials, effort, дату и результат. Проверить ответ по-русски и хотя бы один реальный цикл «модель → синтетический MCP → модель»; реальные Telegram-получатели и production MCP не нужны. Если разрешения или ключей нет, отметить «live API не проверялся», не выдавать offline-тесты за подтверждение live-доступности.

## Итоговый checklist для принимающего агента

- [ ] `openrouter` и `zai` работают как primary и fallback, включая разные модели одного провайдера.
- [ ] Новые настройки изолированы; ключи активных целей обязательны, неактивных — нет; embeddings остались отдельными.
- [ ] Оба адаптера используют Chat Completions, корректные URL/headers/timeout и общую retry-политику.
- [ ] Текст, история, FAQ и несколько MCP-итераций проходят; malformed tool call не выполняется.
- [ ] Reasoning соответствует таблице, сохраняется в родном tool loop, не попадает в пользовательский ответ и не передаётся другой модели при fallback.
- [ ] Ошибки и бизнес-коды классифицируются явно; невалидные ответы не маскируются бессистемным fallback.
- [ ] Завершённые MCP-действия не повторяются, история пишется один раз, usage не теряется.
- [ ] Изображения маршрутизируются только к ранее поддерживавшим их клиентам; новые adapters не отбрасывают картинку молча.
- [ ] TRACE даёт полную диагностику с очищенными секретами, INFO/ERROR не содержат payloads.
- [ ] Все обязательные gates прошли; фактическая область изменений соответствует карте файлов.
- [ ] README и `.env.example` согласованы с реализацией; live-проверки честно отделены от offline.

## Самопроверка плана

Покрытие требований: общий протокол и ownership — Task 1; OpenRouter — Task 2; Z.AI — Task 3; существующие сценарии и fallback — Task 4; наблюдаемость, документация и gates — Task 5. Точки расширения сверены с текущими исходниками; новые типы определены в Interfaces, публичные контракты старых клиентов сохраняются. Ограничения изображений, embeddings и каталога моделей явно записаны, незаполненных решений нет. Исходный код и команды реализации в документ не включены по запросу пользователя; вместо кода тестов приведены конкретные входы, ожидаемые исходы и команды проверки.
