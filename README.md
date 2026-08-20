# VPN Support Bot

Telegram-бот техподдержки VPN-сервиса на Python 3.14+. Принимает текстовые вопросы и скриншоты, ищет ответы в базе знаний (гибридный RAG через PGVector + Full-Text Search), получает данные пользователя через Remnawave (MCP-инструменты) и отвечает через LLM (DeepSeek, Gemini или OpenAI). Форвардит диалоги в форум-группу поддержки с автоэскалацией.

## Архитектура

```
Пользователь → Telegram Bot (aiogram 3) → LLM (DeepSeek/Gemini/OpenAI) ↔ MCP Client (HTTP) → Remnawave
                                  ↕
                      PGVector + FTS (RAG/FAQ)
                                  ↓
                        Форум-группа поддержки
```

- **Стек**: Python 3.14+, aiogram 3.30+, SQLAlchemy 2.0 (asyncpg), pgvector-python, httpx, aiohttp, uv.
- **LLM**: DeepSeek, Gemini или OpenAI (переключается через `LLM_PROVIDER`, список моделей — ниже)
- **MCP**: [mcp-remnawave](https://github.com/mitetenov/mcp-remnawave) 3.2.x по HTTP-транспорту, панель Remnawave 3.3.x. Сервер поднят в режиме support (`REMNAWAVE_IS_SUPPORT=true`): он отдаёт 16 пользовательских инструментов и вырезает VPN-креды из каждого ответа панели. Поверх этого бот сужает список до 5 allow-list инструментов: `users_get_by_telegram_id`, `nodes_list`, `nodes_get`, `hwid_devices_list` и — при `REMNAWAVE_MCP_READONLY=false` — `hwid_device_delete`. Остальные инструменты сервера боту не видны и не вызываемы.
- **RAG**: гибридный поиск по FAQ-базе — векторные эмбеддинги (Gemini/OpenAI) и полнотекстовый поиск PostgreSQL `tsvector` по русскому словарю объединяются через Reciprocal Rank Fusion ($k=60$).
- **Форвардинг**: каждому пользователю — отдельный топик в форум-группе с поддержкой синхронизации реакций.
- **Иллюстрации**: запись FAQ может назвать скриншот полем `image` — файл лежит в `faq/images/` и едет в образе вместе с `faq.json`. Картинка уходит пользователю после текста ответа, когда эта запись встала первой в выдаче, и копируется в топик поддержки, поэтому оператор видит то же, что и пользователь, и может ответить реплаем на неё. Решение принимается по результату поиска, а не маркером от модели, — в промпт не добавляется ни одного токена. Отсутствующий файл пропускается с записью в лог; подробности — в `faq/images/README.md`.
- **Отправка**: весь исходящий трафик идёт через `TelegramMessageSender` — он режет сообщения длиннее 4096 символов по переводам строк и гасит ошибки отправки, чтобы недоставленный ответ не отменял пересылку обращения оператору.

### Защита персональных данных

Аргумент с Telegram ID подставляется маршрутизатором принудительно из ID реального отправителя, а не из того, что вернула модель. Запрос вида «покажи данные для ID 12345» в любом виде — включая промпт-инъекцию — вернёт данные самого спрашивающего.

## Быстрый старт

```bash
git clone https://github.com/mitetenov/SupportAiBot.git && cd SupportAiBot
cp .env.example .env   # заполнить переменные — его читают все три сервиса
docker compose pull
docker compose up -d
docker compose exec support-bot python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
```

Образ: [`mitetenov/supportbot`](https://hub.docker.com/r/mitetenov/supportbot) — ~75 МБ на базе `python:3.14-slim` с установкой зависимостей через `uv`. MCP-сервер — отдельный сервис `mcp-remnawave` в compose.

**Важно**: перед запуском отключите privacy mode бота в BotFather (`/setprivacy` → Disable), иначе бот не будет видеть сообщения в группе.

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `LLM_PROVIDER` | — | `deepseek` | `deepseek`, `gemini` или `openai` |
| `EMBEDDING_PROVIDER` | — | `gemini` | `gemini` или `openai` |
| `TELEGRAM_BOT_TOKEN` | да | — | Токен бота от @BotFather |
| `TELEGRAM_SUPPORT_GROUP_CHAT_ID` | да | — | ID форум-группы (отрицательный, напр. `-1001234567890`) |
| `TELEGRAM_SUPPORT_ADMIN_USERNAME` | — | — | Username админа без `@` для эскалации |
| `TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS` | — | — | Telegram ID админов через запятую для `/stats` и `/gaps` |
| `DEEPSEEK_API_KEY` | при deepseek | — | API-ключ DeepSeek |
| `DEEPSEEK_MODEL` | при deepseek | — | Модель DeepSeek |
| `GEMINI_API_KEY` | при gemini | — | API-ключ Google Gemini |
| `GEMINI_MODEL` | при gemini | — | Модель Gemini |
| `OPENAI_API_KEY` | при openai | — | API-ключ OpenAI |
| `OPENAI_MODEL` | при openai | — | Модель OpenAI (напр. `gpt-5.6-luna`) |
| `OPENAI_EMBEDDING_MODEL` | при embedding=openai | `text-embedding-3-small` | Модель эмбеддингов OpenAI |
| `REMNAWAVE_BASE_URL` | да | — | URL панели Remnawave |
| `REMNAWAVE_API_TOKEN` | да | — | JWT API-токен Remnawave |
| `REMNAWAVE_MCP_READONLY` | — | `false` | Гейт бота: `true` — скрыть от модели все write-операции (удаление HWID-устройств станет недоступно) |
| `REMNAWAVE_IS_SUPPORT` | — | `true` | Гейт MCP-сервера: режим support. Полный доступ ко всем 179 инструментам открывает только точная строка `false` |
| `REMNAWAVE_TIMEOUT_MS` | — | `30000` | Таймаут запросов MCP к панели |
| `PGVECTOR_HOST` | — | `pgvector` | Хост pgvector |
| `PGVECTOR_PORT` | — | `5432` | Порт pgvector |
| `PGVECTOR_USER` | — | `bot` | Пользователь pgvector |
| `PGVECTOR_PASSWORD` | да | — | Пароль pgvector |
| `PGVECTOR_DB` | — | `vpnsupport` | Название БД |
| `BOT_TAG` | — | `latest` | Тег образа mitetenov/supportbot |
| `MCP_TAG` | — | `v3.2.0` | Тег образа mitetenov/remnawave-mcp. Закреплён намеренно: набор инструментов MCP зависит от версии |

При запуске валидируются только переменные выбранного провайдера (ключа и модели). Переменные неактивного провайдера можно не заполнять.

## Команды бота

Команды регистрируются в меню Telegram при старте (`setMyCommands`).

- `/start` — приветствие, сброс истории диалога
- `/help` — что умеет бот и список команд
- `/operator` — эскалация: запрос живого оператора, в группу приходит тег админа
- `/stats` — **только для админов**: топ-10 пользователей по токенам LLM
- `/stats N` — топ-N (N от 1 до 100)
- `/stats TELEGRAM_ID` — статистика конкретного пользователя (prompt/completion/total токены, количество запросов)
- `/gaps` — **только для админов**: статистика топ пробелов в знаниях (запросы без релевантных ответов FAQ)

## Автоэскалация

Админ тегается в форум-группе в двух случаях:

1. **Маркер `[ESCALATE]` от модели** — основной механизм. Модель видит весь диалог и получает в системном промпте явный список поводов: возвраты и отмена подписки, «оплатил, но не продлилось», повторное обращение с той же нерешённой проблемой, явное недовольство, массовая проблема на стороне сервиса, пинг всех серверов `n/a`. Маркер удаляется из текста перед отправкой пользователю.
2. **Прямая просьба о человеке** в сообщении пользователя — слова `оператор`, `человек`, `живой` и их формы. Совпадение идёт по границам слов, поэтому «я живу в Германии» и «болит живот» эскалацию не вызывают.

Команда `/operator` эскалирует всегда и дополнительно фиксирует пробел в знаниях (`USER_OPERATOR`) — сигнал, что предыдущий ответ бота не сработал.

## RAG / База знаний

FAQ хранится в `faq/faq.json`. Каждая запись — `question`, `answer` и `keywords`. При старте бот индексирует их в PGVector (`gemini-embedding-001`, 2000 измерений, либо OpenAI) и в `tsvector`-индекс PostgreSQL.

Поиск гибридный: векторный и полнотекстовый каналы ранжируются независимо и объединяются через **Reciprocal Rank Fusion**.

Эмбеддинги запросов кэшируются (LRU на 256 записей), поэтому повторные и однотипные вопросы не порождают новых обращений к провайдеру.

При использовании готового образа `mitetenov/supportbot` FAQ уже внутри. Для обновления FAQ:

```bash
docker build -t mitetenov/supportbot:latest .
docker compose up -d --force-recreate support-bot
```

## Gemini / OpenAI Vision

При использовании провайдеров с поддержкой модальности изображений (`gemini` или `openai`) бот умеет обрабатывать скриншоты: фото скачивается, конвертируется в base64 и отправляется в LLM вместе с текстовым вопросом.

Вложения других типов (голосовые, видео, документы, стикеры) бот не обрабатывает, но отвечает на них подсказкой и пересылает в топик поддержки — оператор видит обращение целиком.

## Склейка сообщений

Сообщения, отправленные подряд, копятся `telegram.buffer.window` (2.5 с) и уходят в модель одним запросом — человек, который печатает мысль в три сообщения, получает один связный ответ. Буфер сбрасывается досрочно по `telegram.buffer.max-messages` (5). Ограничение частоты применяется к склеенной пачке, а не к каждому сообщению, и при срабатывании обращение всё равно уходит оператору в топик, а не теряется.

## Локальная разработка

Требуется **Python 3.14+**.

Установка зависимостей из lock-файла (те же версии, что и в образе):
```bash
uv sync --extra dev
```

Без `uv` — но тогда версии не закреплены:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Запуск приложения:
```bash
export $(grep -v '^#' .env | xargs)
python3 -m app.main
```

Запуск тестов, линтера и проверки типов — те же команды, что и в merge-gate CI:
```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`pytest` считает покрытие и падает ниже 85% (порог в `pyproject.toml`, тот же локально и в CI).

Зависимости закреплены в `uv.lock`. После правки `pyproject.toml` обновите его командой `uv lock` — CI падает, если файлы разошлись.

## Хранилище

- PostgreSQL 17 + PGVector — маппинг пользователь↔топик, гибридный FAQ-поиск (векторы + FTS)
- Docker volume `pgvector-data` для персистентности
- Схема создаётся при старте (`create_all` + явный DDL для `faq` и `knowledge_gaps`). Миграций нет: существующие таблицы не изменяются, поэтому изменение модели на живой базе нужно применять руками.

## Фоновые задачи

Три очистки крутятся всё время, пока бот жив:

| Задача | Период | Что делает |
|---|---|---|
| `chat-history-eviction` | 1 ч | Удаляет сообщения старше `CHAT_HISTORY_TTL_DAYS` и выгружает неактивные диалоги из памяти |
| `rate-limiter-eviction` | 10 мин | Чистит записи rate-limiter'а |
| `conversation-state-eviction` | 15 мин | Чистит просроченные последние запросы и метки активности оператора |

## Выбор LLM

Переключение — переменная `LLM_PROVIDER` и модель в `.env`. Менять без пересборки, только рестарт.

| Провайдер | Модели |
|---|---|
| **DeepSeek** | `deepseek-v4-flash`, `deepseek-v4-pro` |
| **Gemini** | `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` |
| **OpenAI** | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |

| | DeepSeek | Gemini | OpenAI |
|---|---|---|---|
| Текст и tool calling | ✓ | ✓ | ✓ |
| Изображения (скриншоты) | ✗ | ✓ | ✓ |
| Эмбеддинги (для FAQ) | ✗ | ✓ | ✓ |
