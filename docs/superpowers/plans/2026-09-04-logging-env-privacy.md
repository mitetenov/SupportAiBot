# Three-level logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development inside the single Antigravity controller mandated by sol-flash-sdd. Implementers and internal reviewers are native self, Model inherit. Sol writes no implementation code. Prose-only planning is intentional. Human approval of this exact revision and spec is required before development.

**Goal:** Один BOT_LOG_LEVEL в .env выбирает TRACE со всей диагностикой, INFO с рабочими событиями или ERROR только с ошибками.

**Architecture:** Стандартный Python logging с пользовательским TRACE, общим консольным handler и семантическим распределением событий. Явная инструментация транспортных и прикладных границ фиксирует payloads только на TRACE; INFO/ERROR формируются из безопасных metadata. Небольшой contextvars-контекст связывает операции, отдельный helper очищает credential secrets без HMAC.

**Tech Stack:** Python 3.14+, logging/contextvars, Pydantic Settings, существующие httpx/aiogram/aiohttp/MCP SDK/SQLAlchemy, pytest/ruff/mypy. Без новых runtime dependencies.

**Spec:** docs/superpowers/specs/2026-09-04-logging-env-privacy-design.md (редакция 2).

## Global Constraints

- Только TRACE, INFO и ERROR; единственный новый env-параметр BOT_LOG_LEVEL, default INFO. TRACE включает INFO/ERROR, INFO включает ERROR. Не добавлять настройку DEBUG/WARNING/CRITICAL.
- Удалённые из первого плана logging privacy modes, HMAC key/псевдонимы/identity CLI, content flag, format/length/dependency knobs не реализуются. Существующая HMAC-проверка подписи webhook не меняется.
- TRACE не обрезает текстовые payloads и существующие base64 JSON-поля. Для отдельного двоичного потока — метаданные вместо дампа байтов. INFO/ERROR исключают content/PII, credential secrets скрываются всегда.
- Покрыть все взаимодействия со стороны supportBot; внутренние операции отдельных MCP/DB/proxy процессов вне области работ. Не изменять другие репозитории, бизнес-логику и сетевые контракты.
- Последний remote master — обязательная база development branch; один checkout, plan/spec и Flash controller. Не запускать код до явного одобрения этой редакции. Не использовать живой бот или платные API для проверки.
- Worktree /Users/mikhail/supportBot/.worktrees/logging-env-privacy. Посторонний untracked план в исходном checkout не трогать. Локальную .env не копировать и не коммитить; изменяется только BOT_LOG_LEVEL после реализации.
- Исходный suite: 1087 passed, покрытие 89.80%. Обязательный минимум 85% не снижать.

### Task 1: Три уровня, настройка и общий вывод

**Files:** изменить app/config.py, app/main.py, tests/conftest.py, tests/test_config.py, tests/test_main.py; создать app/logging_config.py, app/logging_redaction.py, app/logging_context.py, tests/test_logging_config.py, tests/test_logging_redaction.py, tests/test_logging_context.py; начать .env.example.

**Responsibilities and interfaces:** Settings предоставляет единственный нормализованный logging level. logging_config регистрирует TRACE ниже DEBUG и настраивает общий выход и маршрутизацию зависимостей. logging_redaction очищает credentials в копиях строк/полей и предоставляет безопасные error metadata. logging_context предоставляет случайные operation/request ID и сброс контекста. Последующие задачи используют эти компоненты, не создавая альтернативных handlers и флагов.

- [ ] Описать тестами и реализовать default INFO, case-insensitive три значения, пробелы, отказ для пустого/неизвестного/старого уровня. Расширить изоляцию env префиксом BOT_LOG_. Проверить отказ до network без input_value и утечки credentials.
- [ ] Реализовать TRACE API стандартного logging, кумулятивные пороги и единый текстовый вывод с UTC и экранированием управляющих символов. Итоговые labels только TRACE/INFO/ERROR. Идемпотентность setup и корректная повторная смена уровня обязательны.
- [ ] Настроить зависимости: подробные legacy DEBUG/INFO/WARNING переводятся в TRACE, ERROR/CRITICAL дают безопасный ERROR и при необходимости подробный TRACE. Не допустить SQL, Telegram Update, HTTP response и произвольного exception text на INFO/ERROR через сторонние handlers.
- [ ] Реализовать обязательную очистку credentials во всех уровнях без маскирования обычной PII на TRACE; проверить URL userinfo/query, Authorization/cookies, nested keys, известные secret values, отсутствие мутации payload и безопасные ошибки formatter/serializer на stderr.
- [ ] Реализовать контекст операций и отдельных попыток. Проверить параллельные обращения, ошибку, отмену, вложенные задачи и долгоживущий worker; correlation ID не является постоянным ID пользователя.
- [ ] Выполнить целевые проверки и сохранить законченный результат в commit.

### Task 2: TRACE обмена с LLM, MCP, Bedolaga и Telegram

**Files:** создать app/logging_http.py и tests/test_logging_transport.py; изменить app/main.py, app/retry.py, app/llm/base.py, app/llm/openai_client.py, app/llm/deepseek.py, app/llm/groq.py, app/llm/gemini.py, app/llm/fallback.py, app/llm/mcp_client.py, app/llm/mcp_router.py, app/bedolaga/client.py, app/bedolaga/webhook.py, app/bot/router.py, app/bot/sender.py, app/bot/photo_downloader.py; дополнить tests/test_openai_client.py, tests/test_deepseek_client.py, tests/test_groq_client.py, tests/test_gemini_client.py, tests/test_mcp_client.py, tests/test_bedolaga_client.py, tests/test_bedolaga_webhook.py, tests/test_sender.py и tests/test_logging_transport.py.

**Responsibilities and interfaces:** logging_http предоставляет общую диагностику HTTP для принадлежащего боту httpx-клиента с paired request/response/attempt ID. Для aiogram и MCP использовать поддерживаемые middleware/hooks и прикладные границы существующих клиентов; один httpx hook не покрывает все эти каналы. Наблюдение не меняет чтение потоков, retries, exception propagation или владельца MCP session.

- [ ] Зафиксировать mocks для полного текстового request/response, query/headers, status/duration и каждой retry/fallback попытки. Проверить большой body значительно длиннее 2000 символов, nested JSON, base64 в уже подготовленном JSON, invalid JSON, non-2xx и timeout. На INFO/ERROR тела отсутствуют и не сериализуются для TRACE.
- [ ] Реализовать TRACE фактического окончательного LLM request body и возвращённого ответа всех четырёх провайдеров: system/developer/user/history, FAQ context, effort, tool schemas/calls/results и provider-returned reasoning. Не добавлять выдуманные скрытые рассуждения. Удалить прежнее сокращение MCP result для TRACE.
- [ ] Покрыть MCP initialize, все страницы list-tools, call-tool и reconnect логическими запросами/результатами SDK. Добавить transport details через поддерживаемые точки расширения там, где доступны, без monkeypatch протокола. Не терять is_error и ошибки до/после соединения.
- [ ] Покрыть прямой Bedolaga API и входящий webhook, Telegram updates и исходящие API методы, включая операции, которые минуют TelegramMessageSender, команды, реакции, getFile и отправку вложений. Существующий signature check не менять.
- [ ] Потоковые/binary uploads/downloads не вычитывать ради лога: фиксировать метаданные и результат. Текстовые bodies, которые приложение уже читает, сохранять полными в TRACE. Проверить отсутствие двойного чтения, лишнего API-вызова и изменений оригинального payload.
- [ ] Устранить бессмысленное дублирование полного body на нескольких слоях, сохранив достаточное представление каждого фактического обмена. Выполнить целевые проверки и сохранить результат в commit.

### Task 3: INFO, ERROR и перераспределение существующих записей

**Files:** изменить app/main.py, app/config.py, app/llm/base.py, app/llm/fallback.py, app/llm/openai_client.py, app/llm/deepseek.py, app/llm/groq.py, app/llm/gemini.py, app/llm/mcp_client.py, app/llm/mcp_router.py, app/retry.py, app/rag/service.py, app/rag/embedding.py, app/rag/initializer.py, app/rag/knowledge_gaps.py, app/storage/database.py, app/storage/chat_history.py, app/storage/schema.py, app/bot/pipeline.py, app/bot/operator_ask.py, app/bot/router.py, app/bot/buffer.py, app/bot/sender.py, app/bot/forwarder.py, app/bot/topic_manager.py, app/bot/photo_downloader.py, app/bot/typing.py, app/bot/admin_notifier.py, app/bot/command_handler.py, app/bot/maintenance.py, app/bedolaga/__init__.py, app/bedolaga/pipeline.py, app/bedolaga/client.py, app/bedolaga/poller.py, app/bedolaga/relay.py, app/bedolaga/webhook.py; создать tests/test_logging_integration.py и дополнить профильные tests/test_pipeline.py, tests/test_bedolaga_pipeline.py, tests/test_llm_fallback.py, tests/test_mcp_router.py, tests/test_faq_service.py, tests/test_chat_history.py.

**Responsibilities and interfaces:** Определить события INFO/ERROR явно на уровне приложения, используя output/context Task 1 и подробный TRACE Task 2. Фактический effort берётся из provider-specific mapping, а не угадывается из общей настройки. Видимость инструментов определяется существующим router. RAG/storage дополняют транспортную диагностику запросами, параметрами и уже прочитанными результатами.

- [ ] Покрыть INFO старта/остановки, selected provider/model/configured+effective effort и fallback transition. Проверить providers с mapping/ignored/unsupported effort и запрет выдавать configured effort за фактически переданный. Само переключение — INFO; произошедший сбой — отдельный ERROR.
- [ ] На INFO зафиксировать факт MCP-вызова и RAG-поиска, имя операции/tool/server, outcome, duration и counts без аргументов и результата. Выводить полные описания загруженных инструментов при init/изменении набора с обозначением доступности модели после allowlist/collisions. Input schema — только TRACE; descriptions не дублируются на каждом вызове.
- [ ] Для RAG и БД добавить полный прикладной TRACE запросов, SQL parameters, истории/FAQ-кандидатов и результатов, которые уже прочитаны приложением. SQLAlchemy logger/echo не должен обходить общий порог и публиковать SQL на INFO. Не потреблять result cursors повторно.
- [ ] Для пользовательских pipeline, /ask и тикетов подключить контекст с корректным сбросом. Перенести личные идентификаторы, названия топиков, имена файлов, содержимое сообщений и routine buffer/typing/cache события в TRACE.
- [ ] Аудировать все существующие logging/print/traceback по app. Бывшие WARNING классифицировать по смыслу: настоящий неуспех в ERROR, значимое штатное изменение состояния в INFO, подробности в TRACE. Аналогично перераспределить DEBUG/CRITICAL; не добавлять новые публичные уровни.
- [ ] На ERROR оставить полезную сводку реальной ошибки: компонент/операция, понятная безопасная причина, class/code/status и расположение кадров. Свободный exception text, response body, SQL parameters и payload — в TRACE. Покрыть cause/context/ExceptionGroup, MCP is_error и malformed webhook. Штатная отмена при остановке и отсутствие необязательных данных не являются ERROR.
- [ ] Проверить выход всех трёх уровней на синтетической PII и credentials, ранние ошибки Settings, сторонние logger, параллельные операции и типичные успешные/ошибочные сценарии. Выполнить профильные проверки и сохранить результат в commit.

### Task 4: Документация, локальная .env и итоговая проверка

**Files:** изменить README.md и .env.example; завершить tests/test_logging_integration.py. Вне commit обновить только BOT_LOG_LEVEL в /Users/mikhail/supportBot/.env.

**Responsibilities and interfaces:** Документация описывает реально реализованный единый переключатель и точное наполнение уровней. Политика доступа к подробностям полностью определяется выбранным уровнем, без HMAC или скрытого content opt-in.

- [ ] Описать три кумулятивных уровня с конкретными событиями пользователя. Default и обычная эксплуатация — INFO; локальная отладка тестового бота — TRACE; минимальный журнал — ERROR. Изменение требует перезапуска.
- [ ] Явно указать, что TRACE содержит полную тестовую переписку и персональные данные, маскирует только credentials, не обрезает текстовые payloads и уже подготовленные base64 JSON-поля; отдельно передаваемые бинарные файлы отражаются метаданными. Охват — обмен со стороны supportBot, а не внутренние операции других контейнеров.
- [ ] Установить BOT_LOG_LEVEL в TRACE в исходной локальной .env, сохранив все остальные значения. Не запускать бот, не читать секреты в отчёт, не копировать env в worktree и не включать в commit. В .env.example оставить INFO.
- [ ] Пройти полный offline pytest с порогом 85%, ruff check, ruff format check и mypy по CI. Проверить итоговый diff: нет бизнес-изменений, настоящих секретов, новых HMAC logging helpers и лишних env-переключателей.
- [ ] Сохранить код и документацию в commit. Завершить независимые внутренние spec/code-quality reviews и final Gemini review в том же controller. Передать HEAD локальному циклу финального Sol review; исправления выполняет Flash в той же ветке. Не push, merge или deploy без отдельного разрешения.

## Self-review редакции 2

Три уровня/env и нормализация logger — Task 1; полный TRACE транспорта и LLM/MCP/Telegram/Bedolaga — Task 2; точный INFO-контракт, ERROR, RAG/DB и классификация всех старых логов — Task 3; документация/local env и gates — Task 4. Убраны все прежние требования logging HMAC, privacy/content switches и truncation. Существующая безопасность webhook не затронута. Все тестовые сценарии описаны прозой по override sol-flash-sdd. Эта редакция заменяет первую и ожидает отдельного одобрения пользователя до разработки.
