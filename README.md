# VPN Support Bot

Telegram-бот техподдержки VPN-сервиса. Принимает текстовые вопросы и скриншоты, ищет ответы в базе знаний (гибридный RAG через PGVector + Full-Text Search), получает данные пользователя через Remnawave (MCP-инструменты) и отвечает через LLM (DeepSeek, Gemini или OpenAI). Форвардит диалоги в форум-группу поддержки с автоэскалацией.

## Архитектура

```
Пользователь → Telegram Bot → LLM (DeepSeek/Gemini/OpenAI) ↔ MCP Client (HTTP) → Remnawave
                                 ↕
                            PGVector + FTS (RAG/FAQ)
                                 ↓
                         Форум-группа поддержки
```

- **LLM**: DeepSeek, Gemini или OpenAI (переключается через `LLM_PROVIDER`, список моделей — ниже)
- **MCP**: [mcp-remnawave](https://github.com/TrackLine/mcp-remnawave) по HTTP-транспорту. Модели доступны только 5 allow-list инструментов: `users_get_by_telegram_id`, `nodes_list`, `nodes_get`, `hwid_devices_list` и — при `REMNAWAVE_READONLY=false` — `hwid_device_delete`. Остальные инструменты сервера боту не видны и не вызываемы.
- **RAG**: гибридный поиск по FAQ-базе — векторные эмбеддинги (Gemini/OpenAI) и полнотекстовый поиск PostgreSQL `tsvector` по русскому словарю объединяются через Reciprocal Rank Fusion
- **Форвардинг**: каждому пользователю — отдельный топик в форум-группе

### Защита персональных данных

Аргумент с Telegram ID подставляется маршрутизатором принудительно из ID реального отправителя, а не из того, что вернула модель. Запрос вида «покажи данные для ID 12345» в любом виде — включая промпт-инъекцию — вернёт данные самого спрашивающего.

## Быстрый старт

```bash
git clone https://github.com/mitetenov/SupportAiBot.git && cd SupportAiBot
cp .env.example .env   # заполнить переменные
docker compose pull
docker compose up -d
docker compose exec support-bot wget -qO- http://localhost:8080/actuator/health
```

Образ: [`mitetenov/supportbot`](https://hub.docker.com/r/mitetenov/supportbot) — 237 МБ, включает FAQ-базу и урезанный через `jlink` Java-рантайм только с нужными модулями. Не требует Java/Maven на хосте. MCP-сервер — отдельный сервис `mcp-remnawave` в compose.

Образ собирается послойно: зависимости (63 МБ) лежат отдельно от кода приложения (700 КБ), поэтому пересборка после правки кода занимает секунды и заливает в реестр меньше мегабайта.

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
| `OPENAI_MODEL` | при openai | — | Модель OpenAI (напр. `gpt-5.5-mini`) |
| `OPENAI_EMBEDDING_MODEL` | при embedding=openai | `text-embedding-3-small` | Модель эмбеддингов OpenAI |
| `REMNAWAVE_BASE_URL` | да | — | URL панели Remnawave |
| `REMNAWAVE_API_TOKEN` | да | — | JWT API-токен Remnawave |
| `REMNAWAVE_READONLY` | — | `false` | `true` — скрыть от модели все write-операции (удаление HWID-устройств станет недоступно) |
| `PGVECTOR_HOST` | — | `pgvector` | Хост pgvector |
| `PGVECTOR_PORT` | — | `5432` | Порт pgvector |
| `PGVECTOR_USER` | — | `bot` | Пользователь pgvector |
| `PGVECTOR_PASSWORD` | да | — | Пароль pgvector |
| `PGVECTOR_DB` | — | `vpnsupport` | Название БД |
| `BOT_TAG` | — | `latest` | Тег образа mitetenov/supportbot |

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

FAQ хранится в `bot/src/main/resources/faq/faq.json` и вшит в JAR при сборке. Каждая запись — `question`, `answer` и `keywords`. При старте бот индексирует их в PGVector (`gemini-embedding-001`, 2000 измерений, либо OpenAI) и в `tsvector`-индекс PostgreSQL.

Поиск гибридный: векторный и полнотекстовый каналы ранжируются независимо и объединяются через **Reciprocal Rank Fusion**. Это существенно: при взвешенной сумме баллов реальные значения `ts_rank` (~0.05) никогда не перевешивали порог, и запись, найденная только по ключевому слову, до модели не доходила.

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

Требуется **JDK 21**. На JDK 22+ inline mock maker Mockito не может инструментировать классы и весь набор тестов падает, поэтому версия проверяется в фазе `validate` с понятным сообщением.

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
export $(grep -v '^#' .env | xargs)
mvn -pl bot spring-boot:run
```

Тесты:

```bash
JAVA_HOME=$(/usr/libexec/java_home -v 21) mvn -pl bot test
```

## Хранилище

- PostgreSQL 17 + PGVector — маппинг пользователь↔топик, гибридный FAQ-поиск (векторы + FTS)
- Docker volume `pgvector-data` для персистентности

## Выбор LLM

Переключение — переменная `LLM_PROVIDER` и модель в `.env`. Менять без пересборки, только рестарт.

| Провайдер | Модели |
|---|---|
| **DeepSeek** | `deepseek-v4-flash`, `deepseek-v4-pro` |
| **Gemini** | `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-omni-flash` |
| **OpenAI** | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.5-mini` |

| | DeepSeek | Gemini | OpenAI |
|---|---|---|---|
| Текст и tool calling | ✓ | ✓ | ✓ |
| Изображения (скриншоты) | ✗ | ✓ | ✓ |
| Эмбеддинги (для FAQ) | ✗ | ✓ | ✓ |
