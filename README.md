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
cp .env.example .env   # заполнить переменные
docker compose up -d --build
docker compose exec bot curl -f http://localhost:8080/actuator/health
```

**Важно**: перед запуском отключите privacy mode бота в BotFather (`/setprivacy` → Disable), иначе бот не будет видеть сообщения в группе.

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `LLM_PROVIDER` | — | `deepseek` | `deepseek` или `gemini` |
| `TELEGRAM_BOT_TOKEN` | да | — | Токен бота от @BotFather |
| `TELEGRAM_SUPPORT_GROUP_CHAT_ID` | да | — | ID форум-группы (отрицательный, напр. `-1001234567890`) |
| `TELEGRAM_SUPPORT_ADMIN_USERNAME` | — | — | Username админа без `@` для эскалации |
| `DEEPSEEK_API_KEY` | при deepseek | — | API-ключ DeepSeek |
| `DEEPSEEK_MODEL` | при deepseek | — | Модель DeepSeek |
| `GEMINI_API_KEY` | при gemini | — | OAuth-токен Google Gemini |
| `GEMINI_MODEL` | при gemini | — | Модель Gemini |
| `REMNAWAVE_BASE_URL` | да | — | URL панели Remnawave |
| `REMNAWAVE_API_TOKEN` | да | — | JWT API-токен Remnawave |
| `REMNAWAVE_READONLY` | — | `true` | `false` — разрешить удаление HWID-устройств |
| `PG_USER` | — | `bot` | Пользователь PostgreSQL |
| `PG_PASSWORD` | да | — | Пароль PostgreSQL |

При запуске валидируются только переменные выбранного провайдера (ключа и модели). Переменные неактивного провайдера можно не заполнять.

## Команды бота

- `/start` — приветствие, сброс истории диалога
- `/operator` — эскалация: запрос живого оператора, в группу приходит тег админа

## Автоэскалация

Бот автоматически тегает админа в форум-группе при обнаружении в сообщении пользователя или ответе бота ключевых слов:

| Триггер в сообщении | Триггер в ответе |
|---|---|
| `отмен*`, `верни*`, `возврат`, `refund`, `жалоб*` | `не удалось`, `ошибк*` |
| `оператор`, `человек`, `жив*` | `не найден`, `попробуйте позже` |
| | `обратитесь` |

## RAG / База знаний

FAQ хранится в `bot/src/main/resources/faq/faq.json`. При старте бот индексирует вопросы через Gemini embeddings (`gemini-embedding-001`, 3072 измерения) в PGVector. При каждом запросе пользователя релевантные FAQ-ответы добавляются в контекст LLM.

Для обновления FAQ без пересборки всего образа (кэшируются слои Maven):

```bash
docker compose build bot
docker compose up -d --force-recreate bot
```

## Gemini Vision

При провайдере `gemini` бот умеет обрабатывать скриншоты: фото скачивается, конвертируется в base64 и отправляется в Gemini вместе с текстовым вопросом.

## Сборка с фиксированной версией MCP

```bash
docker build --build-arg MCP_REF=<commit-or-tag> -t vpn-support-bot .
```

## Локальный запуск

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
