# VPN Support Bot

Telegram-бот техподдержки VPN-сервиса. Принимает текстовые вопросы и скриншоты, ищет ответы в базе знаний (RAG через PGVector), получает данные пользователя через Remnawave (MCP-инструменты) и отвечает через LLM (DeepSeek или Gemini). Форвардит диалоги в форум-группу поддержки с автоэскалацией.

## Архитектура

```
Пользователь → Telegram Bot → LLM (DeepSeek/Gemini) ↔ MCP Client (stdio) → Remnawave
                                ↕
                           PGVector (RAG/FAQ)
                                ↓
                        Форум-группа поддержки
```

- **LLM**: DeepSeek V4 Flash или Gemini 2.5 Flash (переключается через `LLM_PROVIDER`)
- **MCP**: 153 инструмента Remnawave через [mcp-remnawave](https://github.com/TrackLine/mcp-remnawave) (stdio-транспорт)
- **RAG**: семантический поиск по FAQ-базе (Gemini embeddings 3072d + PGVector)
- **Форвардинг**: каждому пользователю — отдельный топик в форум-группе

## Быстрый старт

```bash
git clone https://github.com/mitetenov/SupportAiBot.git && cd SupportAiBot
cp .env.example .env   # заполнить переменные
./run.sh               # загрузка образа и запуск
docker compose exec bot curl -f http://localhost:8080/actuator/health
```

Или вручную без скрипта:
```bash
cp .env.example .env   # заполнить переменные
docker compose pull
docker compose up -d
```

Образ: [`mitetenov/supportbot`](https://hub.docker.com/r/mitetenov/supportbot) — включает JRE, MCP-сервер и FAQ-базу. Не требует Java/Maven/Node.js на хосте.

**Важно**: перед запуском отключите privacy mode бота в BotFather (`/setprivacy` → Disable), иначе бот не будет видеть сообщения в группе.

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `LLM_PROVIDER` | — | `deepseek` | `deepseek` или `gemini` |
| `TELEGRAM_BOT_TOKEN` | да | — | Токен бота от @BotFather |
| `TELEGRAM_SUPPORT_GROUP_CHAT_ID` | да | — | ID форум-группы (отрицательный, напр. `-1001234567890`) |
| `TELEGRAM_SUPPORT_ADMIN_USERNAME` | — | — | Username админа без `@` для эскалации |
| `TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS` | — | — | Telegram ID админов через запятую для `/stats` |
| `DEEPSEEK_API_KEY` | при deepseek | — | API-ключ DeepSeek |
| `DEEPSEEK_MODEL` | при deepseek | — | Модель DeepSeek |
| `GEMINI_API_KEY` | при gemini | — | OAuth-токен Google Gemini |
| `GEMINI_MODEL` | при gemini | — | Модель Gemini |
| `REMNAWAVE_BASE_URL` | да | — | URL панели Remnawave |
| `REMNAWAVE_API_TOKEN` | да | — | JWT API-токен Remnawave |
| `REMNAWAVE_READONLY` | — | `true` | `false` — разрешить удаление HWID-устройств |
| `POSTGRES_HOST` | — | `pgvector` | Хост PostgreSQL |
| `POSTGRES_PORT` | — | `5432` | Порт PostgreSQL |
| `POSTGRES_USER` | — | `bot` | Пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | да | — | Пароль PostgreSQL |
| `POSTGRES_DB` | — | `vpnsupport` | Название БД |
| `BOT_TAG` | — | `latest` | Тег образа mitetenov/supportbot |

При запуске валидируются только переменные выбранного провайдера (ключа и модели). Переменные неактивного провайдера можно не заполнять.

## Команды бота

- `/start` — приветствие, сброс истории диалога
- `/operator` — эскалация: запрос живого оператора, в группу приходит тег админа
- `/stats` — **только для админов**: топ-10 пользователей по токенам LLM
- `/stats N` — топ-N (N от 1 до 100)
- `/stats TELEGRAM_ID` — статистика конкретного пользователя (prompt/completion/total токены, количество запросов)

## Автоэскалация

Бот автоматически тегает админа в форум-группе при обнаружении в сообщении пользователя или ответе бота ключевых слов:

| Триггер в сообщении | Триггер в ответе |
|---|---|
| `отмен*`, `верни*`, `возврат`, `refund`, `жалоб*` | `не удалось`, `ошибк*` |
| `оператор`, `человек`, `жив*` | `не найден`, `попробуйте позже` |
| | `обратитесь` |

## RAG / База знаний

FAQ хранится в `bot/src/main/resources/faq/faq.json` и вшит в JAR при сборке. При старте бот индексирует вопросы через Gemini embeddings (`gemini-embedding-001`, 2000 измерений) в PGVector.

При использовании готового образа `mitetenov/supportbot` FAQ уже внутри. Для обновления FAQ:

```bash
docker build -t mitetenov/supportbot:latest .
docker compose up -d --force-recreate bot
```

## Gemini Vision

При провайдере `gemini` бот умеет обрабатывать скриншоты: фото скачивается, конвертируется в base64 и отправляется в Gemini вместе с текстовым вопросом.

## Локальная разработка

```bash
export $(grep -v '^#' .env | xargs)
mvn -pl bot spring-boot:run
```

## Хранилище

- PostgreSQL 17 + PGVector — маппинг пользователь↔топик, FAQ-эмбеддинги
- Docker volume `pg-data` для персистентности

## Выбор LLM

Переключение — переменная `LLM_PROVIDER` и модель в `.env`. Менять без пересборки, только рестарт.

| Провайдер | Модели |
|---|---|
| **DeepSeek** | `deepseek-v4-flash`, `deepseek-chat` |
| **Gemini** | `gemini-2.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`, `gemini-2.5-pro`, `gemini-flash-latest` |

| | DeepSeek | Gemini |
|---|---|---|
| Текст и tool calling | ✓ | ✓ |
| Изображения (скриншоты) | ✗ | ✓ |
| Эмбеддинги (для FAQ) | ✗ | только Gemini |
