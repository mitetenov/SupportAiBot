# VPN Support Bot

Telegram-бот первой линии поддержки VPN-сервиса. Он отвечает на типовые
вопросы, помогает проверить подписку и подключение, принимает скриншоты и при
необходимости передаёт диалог живому оператору.

Бот работает с обращениями из Telegram и, опционально, с тикетами из кабинета
[Bedolaga](https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot).

## Возможности

- Ответы по базе знаний сервиса.
- Персональная помощь по данным пользователя из Remnawave.
- Работа со скриншотами при использовании модели с поддержкой изображений.
- Отдельный топик для каждого пользователя в форум-группе поддержки.
- Передача сложных обращений оператору без параллельных ответов от бота.
- Команда оператора для запроса ответа у модели прямо из топика поддержки.
- Статистика использования и список частых вопросов, на которые не хватило
  базы знаний.
- Автоматическая обработка тикетов Bedolaga.

Персональные данные всегда запрашиваются для реального отправителя сообщения.
Пользователь или модель не могут подставить чужой Telegram ID и получить данные
другого аккаунта.

## Как проходит обращение

1. Пользователь пишет боту или создаёт тикет в кабинете.
2. Бот учитывает текущий вопрос, недавнюю историю диалога и базу знаний.
3. Если Telegram пользователя связан с Remnawave, бот может проверить его
   подписку, устройства и доступность серверов.
4. Ответ отправляется пользователю, а копия обращения появляется в его топике
   в группе поддержки.
5. Если нужен человек, оператор получает уведомление и продолжает диалог.
   Пока оператор общается с пользователем, бот не вмешивается.

Несколько коротких сообщений, отправленных подряд, обрабатываются как один
вопрос. Голосовые сообщения, видео, документы и стикеры бот не распознаёт, но
пересылает оператору вместе с обращением.

## Два MCP: Remnawave и Bedolaga

Бот подключается к двум независимым MCP-серверам. У каждого свой клиент, своя
MCP-сессия и свой allowlist инструментов.

```
LLM ──► McpRouter ──┬──► bedolaga-mcp ──► Bedolaga Bot API
                    │        │  баланс, платежи, покупки, рефералы
                    │        │  (http://bedolaga-mcp:3100, только внутренняя сеть)
                    │
                    └──► mcp-remnawave ──► Remnawave panel
                             │  состояние панели, узлы, HWID
                             │  (http://mcp-remnawave:3100, только внутренняя сеть)
```

`McpRouter` маршрутизирует вызов инструмента к его владельцу: имя доступно
модели, только если его объявил владелец и оно есть в его allowlist. Сессии у
серверов независимые — ошибка инициализации или падение одного MCP не
отключает инструменты другого.

- **Remnawave MCP** — состояние панели: подписка пользователя, узлы, HWID.
  Поднимается всегда.
- **Bedolaga MCP** — персональные данные Bedolaga: баланс, платежи, покупки,
  рефералы. Включается флагом `BEDOLAGA_MCP_ENABLED` (в `.env`) — это личные
  MCP-инструменты для Telegram-поддержки. НЕ путать с `BEDOLAGA_ENABLED`,
  который включает webhook/poller обработку тикетов и прямой Bedolaga Web API
  client (см. раздел «Тикеты Bedolaga»).

Идентичность всегда пинится системой и никогда не приходит от модели:
положительный `telegram_id` из аутентифицированного Telegram update, либо
внутренний `user_id` кабинета для email-only тикета (абсолютное значение его
отрицательного synthetic conversation key). Для email-only тикетов Bedolaga-данные
доступны (пинится внутренний `user_id`), а Remnawave-инструменты панели вернут
`identity_unavailable` — у такого пользователя нет Telegram-идентичности и
доказанной записи в панели.

### Версионная совместимость

| Компонент | Версия |
|---|---|
| supportBot | `2.0.1` |
| bedolaga-mcp | `1.0.0` |
| Bedolaga Bot API (upstream) | commit `49b05d5`, приложение `4.1.0` |
| mcp-remnawave | `v3.2.1` |

Контракт и decision table Bedolaga MCP описаны в `README.md` репозитория
bedolaga-mcp.

### Частичная деградация

После старта, если один MCP умирает, бот продолжает работать на втором:
жив `mcp-remnawave` — работают инструменты панели; жив `bedolaga-mcp` —
работают персональные инструменты. `depends_on: service_healthy` в compose
требует, чтобы оба MCP были здоровы до старта бота — это про порядок запуска,
а не про время жизни. Если Bedolaga MCP не нужен совсем, уберите сервис
`bedolaga-mcp` и его запись в `depends_on` бота: при
`BEDOLAGA_MCP_ENABLED=false` бот его и так не использует.

### Обновление и откат

Каждый MCP — отдельный образ со своим тегом:

- `mitetenov/remnawave-mcp:${MCP_TAG}`
- `mitetenov/bedolaga-mcp:${BEDOLAGA_MCP_TAG}` — публикуется только `:{sha}` и
  `:{version}`, тега `:latest` нет.

Порядок обновления Bedolaga MCP:

```bash
cd /root/supportBot
cp .env .env.pre-bedolaga-mcp
sed -i 's/^BEDOLAGA_MCP_TAG=.*/BEDOLAGA_MCP_TAG=<новый тег>/' .env
docker compose pull bedolaga-mcp
docker compose up -d --wait bedolaga-mcp
# проверьте health и список инструментов (внутренняя сеть), затем бот:
docker compose pull support-bot
docker compose up -d --wait support-bot
```

Сначала разворачивается новый образ Bedolaga MCP и проверяется его health и
набор инструментов, затем — новый supportBot с обновлённым allowlist/промптом.
Откат supportBot (`BOT_TAG` на предыдущий тег) не требует отката Remnawave MCP:
образ Remnawave и его инструменты живут отдельно.

**Rollback интеграции целиком:** выключение `BEDOLAGA_MCP_ENABLED=false`
возвращает бота в Remnawave-only режим — Bedolaga MCP не подключается, его
инструменты исчезают из allowlist. Пользовательская база и финансовые данные не
меняются: Bedolaga MCP read-only и не хранит состояние, а webhook/poller
обработка тикетов (`BEDOLAGA_ENABLED`) управляется отдельным флагом.

### Эксплуатационная граница

- Для MCP создавайте **отдельный** Bedolaga Web API-токен и указывайте его в
  `BEDOLAGA_API_KEY`. Сейчас `bedolaga-mcp` и webhook/poller-интеграция читают
  один и тот же файл `.env` (env_file), поэтому обе используют одну переменную
  `BEDOLAGA_API_KEY`. Не включайте `BEDOLAGA_ENABLED` и MCP одновременно, если
  им нужны разные токены; для параллельной работы заведите отдельную переменную
  для MCP и переопределите ключ в environment сервиса.
- MCP-сервис работает только во внутренней Docker-сети: порт на host не
  публикуется, доступа извне к нему нет.
- У актуальной токен-модели Bedolaga нет scopes: allowlist MCP ограничивает
  поверхность LLM, но не является полной границей, если сам процесс MCP будет
  скомпрометирован.

## Быстрый старт

Перед запуском:

- создайте Telegram-бота через BotFather;
- подготовьте форум-группу поддержки и добавьте в неё бота;
- отключите privacy mode в BotFather (`/setprivacy` → Disable), иначе бот не
  увидит сообщения операторов в топиках;
- подготовьте Remnawave 3.3.x и API-токен;
- получите ключ выбранного LLM-провайдера.

```bash
git clone https://github.com/mitetenov/SupportAiBot.git
cd SupportAiBot
cp .env.example .env
# заполните .env
docker compose pull
docker compose up -d
docker compose exec support-bot python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
```

Если последняя команда завершилась без ошибки, бот запущен.

## Основные настройки

Все сервисы читают файл `.env`. Полный пример находится в `.env.example`.

### Telegram и Remnawave

| Переменная | Обязательна | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | Токен от BotFather |
| `TELEGRAM_SUPPORT_GROUP_CHAT_ID` | да | ID форум-группы поддержки, например `-1001234567890` |
| `TELEGRAM_SUPPORT_ADMIN_USERNAME` | нет | Username оператора без `@`, которого нужно отмечать при эскалации |
| `TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS` | нет | Telegram ID администраторов для `/stats` и `/gaps`, через запятую |
| `REMNAWAVE_BASE_URL` | да | Адрес панели Remnawave |
| `REMNAWAVE_API_TOKEN` | да | API-токен Remnawave |
| `REMNAWAVE_MCP_URL` | да | Адрес сервиса интеграции с Remnawave; в Docker — `http://mcp-remnawave:3100` |
| `REMNAWAVE_MCP_READONLY` | нет | `true` запрещает боту выполнять изменения, например удалять HWID-устройства |
| `REMNAWAVE_IS_SUPPORT` | рекомендуется `true` | Безопасный режим поддержки на стороне сервиса Remnawave |

### Модель и база знаний

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai`, `gemini` или `deepseek` |
| `REASONING_EFFORT` | `none` | Профиль reasoning: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; `auto` не поддерживается |
| `OPENAI_API_KEY` | — | Нужен при `LLM_PROVIDER=openai` |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Модель OpenAI |
| `GEMINI_API_KEY` | — | Нужен при выборе Gemini или Gemini-эмбеддингов |
| `GEMINI_MODEL` | — | Модель Gemini |
| `DEEPSEEK_API_KEY` | — | Нужен при `LLM_PROVIDER=deepseek` |
| `DEEPSEEK_MODEL` | — | Модель DeepSeek |
| `EMBEDDING_PROVIDER` | `gemini` | Провайдер поиска по базе знаний: `gemini` или `openai` |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Модель поиска при `EMBEDDING_PROVIDER=openai` |

Заполнять ключи неиспользуемых LLM-провайдеров не требуется.

`REASONING_EFFORT` — общий профиль, который каждый клиент преобразует в
нативную настройку выбранной модели. Известные несовместимые пары модель/профиль
отклоняются при старте. OpenAI GPT-5.6 принимает `none`, `low`, `medium`,
`high`, `xhigh`, `max`; Gemini преобразует `xhigh`/`max` в `high`, а DeepSeek
преобразует `minimal`–`high` в `high` и `xhigh`/`max` в `max`.

### База данных и образы

| Переменная | По умолчанию | Описание |
|---|---|---|
| `POSTGRES_USER` / `PGVECTOR_USER` | `bot` | Пользователь базы; значения должны совпадать |
| `POSTGRES_PASSWORD` / `PGVECTOR_PASSWORD` | — | Пароль базы; значения должны совпадать |
| `POSTGRES_DB` / `PGVECTOR_DB` | `vpnsupport` | Название базы; значения должны совпадать |
| `PGVECTOR_HOST` | `pgvector` | Адрес базы данных |
| `PGVECTOR_PORT` | `5432` | Порт базы данных |
| `BOT_TAG` | `latest` | Тег образа бота |
| `MCP_TAG` | `v3.2.1` | Тег образа интеграции с Remnawave |
| `BEDOLAGA_MCP_TAG` | `1.0.0` | Тег образа bedolaga-mcp (только `:{sha}` / `:{version}`, без `:latest`) |

## Использование в Telegram

### Команды пользователя

- `/start` — начать новый диалог и сбросить предыдущий контекст.
- `/help` — показать возможности и команды.
- `/operator` — позвать живого оператора.

### Команды администратора

- `/stats` — показать 10 пользователей с наибольшим расходом токенов.
- `/stats N` — показать первые N пользователей, от 1 до 100.
- `/stats TELEGRAM_ID` — показать статистику конкретного пользователя.
- `/stats clear` — удалить всю статистику токенов.
- `/gaps` — показать частые вопросы, на которые не хватило базы знаний.
- `/gaps clear` — удалить накопленный список таких вопросов.

Команды `/stats` и `/gaps` доступны только ID из
`TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS`. Очистка выполняется сразу и необратима.

### Работа оператора

Оператор отвечает пользователю обычным сообщением внутри его топика. После
ответа бот временно перестаёт вести этот диалог, чтобы не перебивать человека.

Команда `/ask <вопрос>` позволяет оператору запросить ответ у модели. Ответ
отправляется пользователю от имени бота и дублируется в топик. Команда полезна,
когда оператор уже ведёт диалог, но хочет воспользоваться базой знаний и
данными Remnawave.

## База знаний

FAQ находится в `faq/faq.json`. Запись может содержать:

- `question` — вопрос;
- `answer` — готовый ответ;
- `keywords` — дополнительные формулировки для поиска;
- `image` — имя иллюстрации из `faq/images/`.

Если для ответа указана иллюстрация, бот отправляет её пользователю после
текста и показывает оператору в топике поддержки.

При использовании готового Docker-образа FAQ уже включён в него. После
изменения FAQ или иллюстраций пересоберите образ и перезапустите бота:

```bash
docker build -t mitetenov/supportbot:latest .
docker compose up -d --wait support-bot
```

## Тикеты Bedolaga

Бот отвечает на обращения, которые пользователи создают через кабинет
Bedolaga. Ответ появляется в тикете от имени поддержки, а пользователь получает
обычное уведомление Bedolaga. Копия обращения и ответа отправляется в
форум-группу поддержки.

### Что получает пользователь и оператор

- Бот отвечает на новые тикеты и новые сообщения в открытых тикетах.
- Для пользователей с привязанным Telegram используются история диалога и
  доступные данные Remnawave.
- Пользователи, зарегистрированные только по email или OAuth, получают общую
  консультацию и персональные данные Bedolaga (баланс, платежи, покупки,
  рефералы — через пининг внутреннего `user_id`), но без данных панели
  Remnawave: её инструменты для такого тикета возвращают `identity_unavailable`.
- Скриншоты учитываются, если выбранная модель поддерживает изображения.
- Когда нужен человек, тикет получает высокий приоритет, а оператор видит
  уведомление в группе поддержки.
- Пока оператор ведёт диалог, бот не вмешивается. После 30 минут без нового
  ответа оператора бот снова может подхватить обращение.
- Бот периодически проверяет открытые тикеты, поэтому пропущенное уведомление
  от Bedolaga не приводит к потере обращения.

### Настройка

> **Перед первым включением разберите старые тикеты.** Бот обработает все
> тикеты в статусах `open` и `pending`, включая старые. Закройте ненужные
> обращения и включайте интеграцию на контролируемой очереди.

1. В админке Bedolaga создайте API-токен и придумайте отдельный секрет для
   вебхуков. Добавьте в `.env` бота:

```env
BEDOLAGA_ENABLED=true
BEDOLAGA_API_URL=https://bedolaga.example.com
BEDOLAGA_API_KEY=your_bedolaga_api_key
BEDOLAGA_WEBHOOK_SECRET=your_random_webhook_secret
BEDOLAGA_WEBHOOK_PATH=/bedolaga/webhook
BEDOLAGA_WEBHOOK_BIND=127.0.0.1
BEDOLAGA_POLL_INTERVAL_SECONDS=60
BEDOLAGA_MAX_CONCURRENT_TICKETS=5
```

`BEDOLAGA_API_URL` должен быть доступен с сервера support-бота. Bedolaga и
support-бот могут находиться на разных серверах.

2. Откройте webhook-порт бота с помощью оверлея:

```bash
docker compose -f docker-compose.yml -f docker-compose.bedolaga.yml up -d
```

По умолчанию порт `8080` публикуется только на `127.0.0.1`. Настройте HTTPS
reverse proxy с публичным или приватным адресом, например
`https://support.example.com/bedolaga/webhook`.

Если сервер Bedolaga обращается к support-боту напрямую по приватной сети,
укажите в `BEDOLAGA_WEBHOOK_BIND` приватный IP сервера support-бота и разрешите
доступ к порту `8080` только с IP сервера Bedolaga.

3. Зарегистрируйте в Bedolaga два вебхука: для нового тикета и нового
   сообщения. Команды выполняйте там, где доступен HTTP API панели.
   `PANEL_URL` — адрес Bedolaga, доступный из вашей консоли, а
   `BOT_WEBHOOK_URL` — адрес support-бота, доступный с сервера Bedolaga.

```bash
PANEL_URL="https://bedolaga.example.com"
API_KEY="your_bedolaga_api_key"
WEBHOOK_SECRET="your_random_webhook_secret"
BOT_WEBHOOK_URL="https://support.example.com/bedolaga/webhook"

curl -X POST "$PANEL_URL/webhooks" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"support-bot: new ticket","url":"'"$BOT_WEBHOOK_URL"'","event_type":"ticket.created","secret":"'"$WEBHOOK_SECRET"'"}'

curl -X POST "$PANEL_URL/webhooks" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"support-bot: ticket message","url":"'"$BOT_WEBHOOK_URL"'","event_type":"ticket.message_added","secret":"'"$WEBHOOK_SECRET"'"}'
```

Если вы изменили `BEDOLAGA_WEBHOOK_PATH`, укажите тот же путь в
`BOT_WEBHOOK_URL`.

4. Откройте тестовый тикет и убедитесь, что ответ появился в кабинете, а копия
   обращения — в форум-группе поддержки.

### Работа оператора с тикетом

- Чтобы ответ был виден в кабинете, отвечайте в самом тикете Bedolaga.
- Ответ из зеркального топика отправляется в личный Telegram только
  пользователям с привязанным Telegram ID и не добавляется в тикет Bedolaga.
- Для аккаунтов без Telegram ID зеркальный топик служит только для уведомлений;
  отвечать таким пользователям нужно через Bedolaga.
- Когда оператор отвечает пользователю, бот временно перестаёт вести этот
  диалог.

## Выбор модели

Провайдер и модель меняются через `.env`; пересобирать образ не нужно, достаточно
перезапустить бота.

| Провайдер | Поддерживаемые модели | Скриншоты | Можно использовать для индекса FAQ |
|---|---|---|---|
| OpenAI | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` | да | да |
| Gemini | `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | да | да |
| DeepSeek | `deepseek-v4-flash`, `deepseek-v4-pro` | нет | нет |

## Локальная разработка

Требуется Python 3.14+ и `uv`.

```bash
uv sync --extra dev
```

Запуск приложения:

```bash
set -a
source .env
set +a
uv run python -m app.main
```

Проверки перед отправкой изменений:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy app
```

## Обновление MCP 3.2.0 → 3.2.1

Порядок миграции на сервере:

```bash
cd /root/supportBot
cp .env .env.pre-mcp-3.2.1
sed -i 's/^MCP_TAG=.*/MCP_TAG=v3.2.1/' .env
docker compose pull mcp-remnawave
docker compose up -d --wait mcp-remnawave
docker compose pull support-bot
docker compose up -d --wait support-bot
```

- Однократное обновление MCP очищает старую singleton-сессию.
- После деплоя обеих версий перезапуск бота `docker compose restart support-bot` безопасен и не перезапускает MCP.
- При работе на v3.2.0 аварийным восстановлением остаётся `docker compose up -d --force-recreate mcp-remnawave support-bot`, но для штатного деплоя оно больше не требуется.
