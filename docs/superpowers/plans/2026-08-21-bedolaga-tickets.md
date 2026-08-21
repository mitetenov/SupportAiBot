# Bedolaga Tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Тикеты, которые пользователи открывают в кабинете и боте Bedolaga, отвечает этот support-бот — той же моделью, тем же FAQ и теми же данными Remnawave, что и Telegram-диалоги.

**Architecture:** Новый пакет `app/bedolaga/` — адаптер к Web API Bedolaga. Входящие вебхуки `ticket.created` / `ticket.message_added` приходят на aiohttp-сервер, который бот уже поднимает для `/health`; отдельный сверочный поллинг раз в минуту добирает то, что вебхук не донёс (WebhookService в Bedolaga не делает ретраев). Каждое событие превращается в `TicketAnswerer.handle(ticket_id)`: тикет читается целиком через API, ответ берётся у существующего `LlmClient` под Telegram ID автора, уходит обратно в тикет через `POST /tickets/{id}/reply` и зеркалится в форум-топик пользователя. Идемпотентность держится таблицей `bedolaga_ticket_state` (последнее отвеченное сообщение тикета).

**Tech Stack:** Python 3.14, httpx (клиент API), aiohttp (приём вебхука), SQLAlchemy 2.0 async (состояние тикетов), pytest + pytest-asyncio, uv.

## Global Constraints

- Python `>=3.14`; зависимости не добавляются — httpx, aiohttp, SQLAlchemy уже в `pyproject.toml`.
- ruff: `line-length = 100`, правила `E,F,W,I,UP,B,C4`; mypy по `files = ["app"]`.
- Порог покрытия `--cov-fail-under=85` — каждая задача добавляет тесты вместе с кодом.
- Код, комментарии и докстринги — по-английски, как в остальном репозитории. Все строки, которые видит пользователь или оператор, живут в `app.constants.MESSAGES` и берутся через `get_message(...)` — по-русски.
- Секреты в `Settings` — только `SecretStr`, чтение через `app.config.reveal(...)`.
- Интеграция выключена по умолчанию: без `BEDOLAGA_ENABLED=true` поведение бота не меняется ни на байт.
- Ветка: `feat/bedolaga-tickets`, коммиты в conventional-стиле (`feat:`, `test:`, `docs:`).

## Проверенные факты о Bedolaga (на 2026-08-21, ветка `main`)

Это контракт, под который пишется код. Всё проверено по исходникам `fr1ngg/remnawave-bedolaga-telegram-bot`, а не по документации — документация местами отстаёт.

| Что | Факт | Где в их коде |
|---|---|---|
| Аутентификация Web API | заголовок `X-API-Key: <token>`, базовый URL — корень их FastAPI (порт 8080) | `app/webapi/dependencies.py: require_api_token` |
| Чтение тикета | `GET /tickets/{id}` → тикет **с сообщениями**, отсортированными по `created_at` | `app/webapi/routes/tickets.py: get_ticket` |
| Поля сообщения тикета | каждый элемент `messages` в ответе `GET /tickets/{id}` сериализуется схемой `TicketMessageResponse`: `id, user_id, message_text, is_from_admin, has_media, media_type, media_file_id, media_caption, created_at`. То есть `has_media` и `media_type` есть **не только в payload'ах вебхуков**, но и на самих сообщениях тикета — на этом держится весь vision-путь (`has_media` + `media_type == "photo"`) | `app/webapi/schemas/tickets.py: TicketMessageResponse`, отдаётся из `app/webapi/routes/tickets.py: get_ticket` и `list_tickets` |
| Список тикетов | `GET /tickets?status=open&limit=50` → массив тикетов **без сообщений** (`include_messages=False`) — годится только для получения id | `app/webapi/routes/tickets.py: list_tickets` |
| Ответ в тикет | `POST /tickets/{id}/reply`, тело `{"message_text": "..."}`, `message_text` ≤ 4000 символов, ответ 201 | `app/webapi/routes/tickets.py: reply_to_ticket` |
| Что делает ответ | пишет сообщение с `is_from_admin=true`, переводит тикет в статус `answered`, **шлёт пользователю уведомление в Telegram** и пушит сообщение в кабинет по WebSocket | `TicketMessageCRUD.add_message`, `notify_user_about_ticket_reply` |
| Приоритет | `POST /tickets/{id}/priority`, тело `{"priority": "high"}`; допустимо `low\|normal\|high\|urgent` | `app/webapi/routes/tickets.py: update_ticket_priority` |
| Пользователь | `GET /users/{user_id}` → есть поле `telegram_id`, **может быть `null`** (регистрация по email/OAuth). `user_id` в тикете — внутренний id Bedolaga, **не Telegram ID** | `app/webapi/routes/users.py: get_user` |
| Медиа сообщения | `GET /tickets/{id}/messages/{mid}/media` → `media_url`; сам файл отдаётся по `GET /media/{file_id}` и **тоже требует `X-API-Key`** | `app/webapi/routes/media.py: download_media` |
| События | `ticket.created`, `ticket.message_added`, `ticket.status_changed` уходят и в WebSocket, и в вебхуки (`emit(..., db=db)`) | `app/database/crud/ticket.py`, `app/cabinet/routes/tickets.py` |
| Payload `ticket.created` | `{ticket_id, user_id, title, status, priority, has_media}` — **текста первого сообщения нет** | `TicketCRUD.create_ticket` |
| Payload `ticket.message_added` | `{ticket_id, message_id, user_id, is_from_admin, message_text, has_media, status}`, где `message_text` **обрезан до 200 символов** | `TicketMessageCRUD.add_message` |
| Заголовки вебхука | `X-Webhook-Event`, `X-Webhook-Id`, `X-Webhook-Signature: sha256=<hex>` — HMAC-SHA256 от тела запроса секретом вебхука | `app/services/webhook_service.py` |
| Ретраи вебхуков | **их нет** — упавшая доставка теряется навсегда | `WebhookService._deliver_webhook_http` |
| Регистрация вебхука | `POST /webhooks` с `{name, url, event_type, secret}`; **один event_type на вебхук**, значит нужно два | `app/webapi/routes/webhooks.py` |
| Статусы | пользовательский ответ из бота → `open`, из кабинета → `pending`; ответ админа → `answered` | `TicketMessageCRUD.add_message`, `app/cabinet/routes/tickets.py` |

Три следствия, которые определяют дизайн:

1. **Payload события недостаточно, чтобы отвечать** (нет текста / обрезан) — после каждого события тикет читается целиком через `GET /tickets/{id}`.
2. **Наш собственный ответ возвращается событием `ticket.message_added` с `is_from_admin=true`** — без фильтра бот отвечал бы сам себе бесконечно.
3. **Вебхук может потеряться** — поэтому сверочный поллинг обязателен, а вся обработка идемпотентна по `(ticket_id, last_message_id)`.

## File Structure

**Новый пакет `app/bedolaga/`** — всё, что знает про чужой API, лежит здесь и больше нигде:

- `app/bedolaga/types.py` — `Ticket`, `TicketMessage`, `ImageAttachment`, разбор JSON панели. Ни одного HTTP-вызова.
- `app/bedolaga/client.py` — `BedolagaClient`: единственное место, которое ходит в Web API Bedolaga.
- `app/bedolaga/state.py` — `TicketStateStore`: что уже отвечено. Единственное место, которое пишет в новую таблицу.
- `app/bedolaga/pipeline.py` — `TicketAnswerer`: один ход диалога по тикету (аналог `UserMessagePipeline` для Telegram).
- `app/bedolaga/webhook.py` — приём и проверка подписи вебхука, ничего больше.
- `app/bedolaga/poller.py` — сверочный обход открытых тикетов.
- `app/bedolaga/__init__.py` — `create_ticket_support(...)`: сборка всего перечисленного, чтобы `main.py` остался списком шагов.

**Изменяемые файлы:** `app/config.py` (настройки), `app/constants.py` (сообщения), `app/storage/models.py` (таблица состояния), `app/main.py` (проводка), `tests/conftest.py` (изоляция новых переменных окружения), `.env.example`, `docker-compose.yml`, `README.md`.

---

### Task 1: Настройки интеграции

**Files:**
- Modify: `app/config.py` (блок полей после `# Remnawave MCP`, и `validate_startup`)
- Modify: `tests/conftest.py:12-24` (кортеж `_SETTINGS_ENV_PREFIXES`)
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `app.config.Settings`, `app.config.reveal`.
- Produces: поля `Settings.bedolaga_enabled: bool`, `bedolaga_api_url: str`, `bedolaga_api_key: SecretStr`, `bedolaga_webhook_secret: SecretStr`, `bedolaga_webhook_path: str`, `bedolaga_poll_interval_seconds: int`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_config.py` в конец файла:

```python
class TestBedolagaSettings:
    """The Bedolaga ticket integration is off until it is fully configured."""

    def test_disabled_by_default(self, valid_settings_dict: dict[str, object]) -> None:
        settings = Settings(**valid_settings_dict)
        assert settings.bedolaga_enabled is False
        assert settings.bedolaga_webhook_path == "/bedolaga/webhook"
        assert settings.bedolaga_poll_interval_seconds == 60

    def test_enabled_requires_api_url(self, valid_settings_dict: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_API_URL"):
            Settings(**valid_settings_dict, bedolaga_enabled=True, bedolaga_api_key="key")

    def test_enabled_requires_api_key(self, valid_settings_dict: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match="BEDOLAGA_API_KEY"):
            Settings(
                **valid_settings_dict,
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
            )

    def test_enabled_with_full_configuration(self, valid_settings_dict: dict[str, object]) -> None:
        settings = Settings(
            **valid_settings_dict,
            bedolaga_enabled=True,
            bedolaga_api_url="http://bedolaga:8080/",
            bedolaga_api_key="secret-token",
        )
        assert settings.bedolaga_enabled is True
        assert reveal(settings.bedolaga_api_key) == "secret-token"
```

Импорты в шапке файла должны включать `reveal` и `ValidationError` — проверить, что они там есть, и дописать при необходимости:

```python
from pydantic import ValidationError

from app.config import Settings, reveal
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/test_config.py::TestBedolagaSettings -v --no-cov`
Expected: FAIL — `Settings` не имеет поля `bedolaga_enabled` (`ValidationError: Extra inputs are not permitted` или `AttributeError`).

- [ ] **Step 3: Добавить поля и валидацию**

В `app/config.py`, сразу после блока `# Remnawave MCP` и до `# Server / Healthcheck`:

```python
    # Bedolaga tickets
    bedolaga_enabled: bool = False
    bedolaga_api_url: str = ""
    bedolaga_api_key: SecretStr = SecretStr("")
    bedolaga_webhook_secret: SecretStr = SecretStr("")
    bedolaga_webhook_path: str = "/bedolaga/webhook"
    bedolaga_poll_interval_seconds: int = 60
```

В теле `validate_startup`, рядом с остальными проверками, добавить:

```python
        if self.bedolaga_enabled:
            if not self.bedolaga_api_url.strip():
                raise ValueError("BEDOLAGA_API_URL is required when BEDOLAGA_ENABLED is true")
            _require_text(
                self.bedolaga_api_key,
                "BEDOLAGA_API_KEY is required when BEDOLAGA_ENABLED is true",
            )
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `.venv/bin/python -m pytest tests/test_config.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Изолировать новые переменные в тестах**

В `tests/conftest.py` добавить в кортеж `_SETTINGS_ENV_PREFIXES` строку `"BEDOLAGA_",` — иначе `.env` разработчика протечёт в тесты валидации.

Run: `.venv/bin/python -m pytest tests/test_config.py -v --no-cov`
Expected: PASS

- [ ] **Step 6: Дописать `.env.example`**

В конец `.env.example`:

```
# Тикеты Bedolaga. Пока false — бот работает только с Telegram-диалогами.
BEDOLAGA_ENABLED=false
# Корень Web API Bedolaga (её FastAPI, порт 8080). Оба контейнера должны
# видеть друг друга по имени — см. общую docker-сеть в docker-compose.yml.
BEDOLAGA_API_URL=http://bedolaga:8080
# Токен Web API: админка Bedolaga → API-токены, или POST /tokens.
BEDOLAGA_API_KEY=your_bedolaga_api_key_here
# Секрет вебхука: тот же самый указывается при создании вебхука в Bedolaga.
# Пустой = подпись не проверяется; так делать не надо, эндпоинт открыт наружу.
BEDOLAGA_WEBHOOK_SECRET=your_webhook_secret_here
# Путь, по которому бот слушает вебхуки на своём порту 8080.
BEDOLAGA_WEBHOOK_PATH=/bedolaga/webhook
# Период сверочного опроса открытых тикетов, секунды. Вебхуки Bedolaga
# не ретраятся — этот опрос добирает потерянные события.
BEDOLAGA_POLL_INTERVAL_SECONDS=60
```

- [ ] **Step 7: Коммит**

```bash
git add app/config.py tests/conftest.py tests/test_config.py .env.example && git commit -m "feat(bedolaga): settings for the ticket integration"
```

---

### Task 2: Модель тикета и разбор ответа API

**Files:**
- Create: `app/bedolaga/__init__.py` (пока пустой докстринг — наполнится в Task 9)
- Create: `app/bedolaga/types.py`
- Test: `tests/test_bedolaga_types.py`

**Interfaces:**
- Consumes: ничего из проекта.
- Produces: `OPEN_STATUSES: frozenset[str]`; `ImageAttachment(base64_image: str, mime_type: str)`; `TicketMessage(id: int, text: str, is_from_admin: bool, has_media: bool = False, media_type: str | None = None)`; `Ticket(id, user_id, title, status, priority="normal", messages: tuple[TicketMessage, ...] = ())` со свойствами `last_message -> TicketMessage | None`, `awaits_answer -> bool`, `question -> str`; функции `message_from_payload(payload) -> TicketMessage`, `ticket_from_payload(payload) -> Ticket`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_types.py`:

```python
"""Unit tests for parsing the Bedolaga ticket payloads."""

from app.bedolaga.types import Ticket, TicketMessage, ticket_from_payload

PAYLOAD = {
    "id": 17,
    "user_id": 55,
    "title": "Не подключается VPN",
    "status": "open",
    "priority": "normal",
    "messages": [
        {
            "id": 100,
            "user_id": 55,
            "message_text": "На телефоне пишет ошибку",
            "is_from_admin": False,
            "has_media": True,
            "media_type": "photo",
        },
        {
            "id": 101,
            "user_id": 55,
            "message_text": "Проверьте подписку",
            "is_from_admin": True,
            "has_media": False,
            "media_type": None,
        },
    ],
}


class TestTicketFromPayload:
    """The API payload becomes the shape the rest of the code reads."""

    def test_parses_ticket_fields(self) -> None:
        ticket = ticket_from_payload(PAYLOAD)
        assert ticket.id == 17
        assert ticket.user_id == 55
        assert ticket.title == "Не подключается VPN"
        assert ticket.status == "open"
        assert ticket.priority == "normal"

    def test_parses_messages_in_order(self) -> None:
        ticket = ticket_from_payload(PAYLOAD)
        assert [m.id for m in ticket.messages] == [100, 101]
        assert ticket.messages[0].text == "На телефоне пишет ошибку"
        assert ticket.messages[0].has_media is True
        assert ticket.messages[0].media_type == "photo"
        assert ticket.messages[1].is_from_admin is True

    def test_tolerates_a_ticket_without_messages(self) -> None:
        ticket = ticket_from_payload({"id": 5, "user_id": 1, "title": "t", "status": "open"})
        assert ticket.messages == ()
        assert ticket.last_message is None
        assert ticket.awaits_answer is False

    def test_tolerates_null_text(self) -> None:
        payload = {
            "id": 5,
            "user_id": 1,
            "title": "t",
            "status": "open",
            "messages": [{"id": 1, "message_text": None, "is_from_admin": False}],
        }
        assert ticket_from_payload(payload).messages[0].text == ""


class TestAwaitsAnswer:
    """Only a ticket whose last word is the user's is ours to answer."""

    def _ticket(self, status: str, *messages: TicketMessage) -> Ticket:
        return Ticket(id=1, user_id=2, title="t", status=status, messages=messages)

    def test_true_when_the_last_message_is_from_the_user(self) -> None:
        ticket = self._ticket("open", TicketMessage(id=1, text="Помогите", is_from_admin=False))
        assert ticket.awaits_answer is True

    def test_true_for_a_pending_ticket_from_the_cabinet(self) -> None:
        ticket = self._ticket(
            "pending", TicketMessage(id=1, text="Ещё вопрос", is_from_admin=False)
        )
        assert ticket.awaits_answer is True

    def test_false_when_support_answered_last(self) -> None:
        ticket = self._ticket(
            "answered",
            TicketMessage(id=1, text="Помогите", is_from_admin=False),
            TicketMessage(id=2, text="Держите", is_from_admin=True),
        )
        assert ticket.awaits_answer is False

    def test_false_for_a_closed_ticket(self) -> None:
        ticket = self._ticket("closed", TicketMessage(id=1, text="Помогите", is_from_admin=False))
        assert ticket.awaits_answer is False


class TestQuestion:
    """The title carries the topic of a ticket nobody has followed up on yet."""

    def test_prepends_the_title_to_the_only_message(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Не работает оплата",
            status="open",
            messages=(TicketMessage(id=1, text="Карта не проходит", is_from_admin=False),),
        )
        assert ticket.question == "Не работает оплата\n\nКарта не проходит"

    def test_does_not_repeat_a_title_the_message_already_contains(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Карта не проходит",
            status="open",
            messages=(
                TicketMessage(id=1, text="Карта не проходит, помогите", is_from_admin=False),
            ),
        )
        assert ticket.question == "Карта не проходит, помогите"

    def test_uses_only_the_latest_message_in_a_running_thread(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Не работает оплата",
            status="open",
            messages=(
                TicketMessage(id=1, text="Карта не проходит", is_from_admin=False),
                TicketMessage(id=2, text="Попробуйте другую", is_from_admin=True),
                TicketMessage(id=3, text="Та же ошибка", is_from_admin=False),
            ),
        )
        assert ticket.question == "Та же ошибка"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_types.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bedolaga'`

- [ ] **Step 3: Написать модуль**

Создать `app/bedolaga/__init__.py`:

```python
"""Answering Bedolaga support tickets with the same model that answers Telegram."""
```

Создать `app/bedolaga/types.py`:

```python
"""The slice of the Bedolaga ticket API this bot reads, as plain data."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Statuses in which a ticket is still waiting for support. The bot writes
#: `open`, the cabinet writes `pending` — both mean the same thing to us.
OPEN_STATUSES: frozenset[str] = frozenset({"open", "pending"})


@dataclass(frozen=True)
class ImageAttachment:
    """A ticket screenshot, encoded the way the vision APIs want it."""

    base64_image: str
    mime_type: str


@dataclass(frozen=True)
class TicketMessage:
    """One message inside a ticket."""

    id: int
    text: str
    is_from_admin: bool
    has_media: bool = False
    media_type: str | None = None


@dataclass(frozen=True)
class Ticket:
    """A ticket with its messages, oldest first — the order the API returns."""

    id: int
    user_id: int
    title: str
    status: str
    priority: str = "normal"
    messages: tuple[TicketMessage, ...] = ()

    @property
    def last_message(self) -> TicketMessage | None:
        """The most recent message, or None for a ticket that somehow has none."""
        return self.messages[-1] if self.messages else None

    @property
    def awaits_answer(self) -> bool:
        """True when the last word is the user's and the ticket is still open."""
        last = self.last_message
        return last is not None and not last.is_from_admin and self.status in OPEN_STATUSES

    @property
    def question(self) -> str:
        """What to ask the model.

        A ticket opened a minute ago is a title plus one message, and the title
        is usually where the actual problem is named ("Не работает оплата" +
        "уже третий раз"). Once the thread is running, the title is stale
        context the chat history already carries, so only the newest message
        counts.
        """
        last = self.last_message
        text = last.text.strip() if last else ""
        title = self.title.strip()
        if len(self.messages) == 1 and title and title.lower() not in text.lower():
            return f"{title}\n\n{text}" if text else title
        return text


def message_from_payload(payload: Mapping[str, Any]) -> TicketMessage:
    """Build a TicketMessage from one element of the API's `messages` array."""
    return TicketMessage(
        id=int(payload.get("id") or 0),
        text=str(payload.get("message_text") or ""),
        is_from_admin=bool(payload.get("is_from_admin")),
        has_media=bool(payload.get("has_media")),
        media_type=payload.get("media_type") or None,
    )


def ticket_from_payload(payload: Mapping[str, Any]) -> Ticket:
    """Build a Ticket from the body of `GET /tickets/{id}`."""
    raw_messages = payload.get("messages") or []
    return Ticket(
        id=int(payload["id"]),
        user_id=int(payload.get("user_id") or 0),
        title=str(payload.get("title") or ""),
        status=str(payload.get("status") or ""),
        priority=str(payload.get("priority") or "normal"),
        messages=tuple(message_from_payload(item) for item in raw_messages),
    )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_types.py -v --no-cov`
Expected: PASS (все 12)

- [ ] **Step 5: Коммит**

```bash
git add app/bedolaga tests/test_bedolaga_types.py && git commit -m "feat(bedolaga): parse tickets from the panel API"
```

---

### Task 3: HTTP-клиент Bedolaga

**Files:**
- Create: `app/bedolaga/client.py`
- Test: `tests/test_bedolaga_client.py`

**Interfaces:**
- Consumes: `app.bedolaga.types` (`OPEN_STATUSES`, `Ticket`, `ticket_from_payload`), `app.retry.post_with_retry`.
- Produces: `BedolagaClient(base_url: str, api_key: str, http_client: httpx.AsyncClient)` с методами
  `async get_ticket(ticket_id: int) -> Ticket | None`,
  `async list_awaiting_ticket_ids(limit: int = 50) -> list[int]`,
  `async reply(ticket_id: int, text: str) -> bool`,
  `async set_priority(ticket_id: int, priority: str) -> bool`,
  `async resolve_telegram_id(user_id: int) -> int | None`;
  константа `MAX_REPLY_LENGTH: int = 4000`.

**Замечание для реализующего:** `app/retry.py` умеет только POST (`post_with_retry`). GET-запросы делаются напрямую через `http_client.get` — упавшее чтение не страшно, его подберёт следующий проход поллера.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_client.py`:

```python
"""Unit tests for BedolagaClient — the only code that talks to the panel API."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.bedolaga.client import MAX_REPLY_LENGTH, BedolagaClient

BASE_URL = "http://bedolaga:8080"
API_KEY = "test-api-key"

TICKET_BODY: dict[str, Any] = {
    "id": 17,
    "user_id": 55,
    "title": "Не подключается",
    "status": "open",
    "priority": "normal",
    "messages": [{"id": 100, "message_text": "Помогите", "is_from_admin": False}],
}


def _response(status_code: int, json_body: Any = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.text = "body"
    return response


def _client(
    get: Any = None,
    post: Any = None,
) -> tuple[BedolagaClient, MagicMock]:
    http_client = MagicMock(spec=httpx.AsyncClient)
    http_client.get = get or AsyncMock(return_value=_response(200, TICKET_BODY))
    http_client.post = post or AsyncMock(return_value=_response(201, {}))
    return BedolagaClient(BASE_URL, API_KEY, http_client), http_client


class TestGetTicket:
    """Reading one ticket, with its messages."""

    async def test_returns_the_parsed_ticket(self) -> None:
        client, http_client = _client()
        ticket = await client.get_ticket(17)
        assert ticket is not None
        assert ticket.id == 17
        assert ticket.messages[0].text == "Помогите"
        url = http_client.get.await_args.args[0]
        assert url == "http://bedolaga:8080/tickets/17"

    async def test_sends_the_api_key(self) -> None:
        client, http_client = _client()
        await client.get_ticket(17)
        assert http_client.get.await_args.kwargs["headers"]["X-API-Key"] == API_KEY

    async def test_returns_none_on_404(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(404, {})))
        assert await client.get_ticket(17) is None

    async def test_returns_none_when_the_panel_is_unreachable(self) -> None:
        client, _ = _client(get=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.get_ticket(17) is None

    async def test_trims_a_trailing_slash_from_the_base_url(self) -> None:
        http_client = MagicMock(spec=httpx.AsyncClient)
        http_client.get = AsyncMock(return_value=_response(200, TICKET_BODY))
        client = BedolagaClient("http://bedolaga:8080/", API_KEY, http_client)
        await client.get_ticket(17)
        assert http_client.get.await_args.args[0] == "http://bedolaga:8080/tickets/17"


class TestListAwaitingTicketIds:
    """The list endpoint answers without messages, so it is good for ids only."""

    async def test_collects_ids_across_open_and_pending(self) -> None:
        get = AsyncMock(
            side_effect=[
                _response(200, [{"id": 1}, {"id": 2}]),
                _response(200, [{"id": 3}]),
            ]
        )
        client, _ = _client(get=get)
        assert sorted(await client.list_awaiting_ticket_ids()) == [1, 2, 3]

    async def test_survives_one_failing_status_query(self) -> None:
        get = AsyncMock(side_effect=[httpx.ConnectError("refused"), _response(200, [{"id": 3}])])
        client, _ = _client(get=get)
        assert await client.list_awaiting_ticket_ids() == [3]


class TestReply:
    """Posting the answer back into the ticket."""

    async def test_posts_the_text_and_reports_success(self) -> None:
        client, http_client = _client()
        assert await client.reply(17, "Проверьте подписку") is True
        url = http_client.post.await_args.args[0]
        assert url == "http://bedolaga:8080/tickets/17/reply"
        assert http_client.post.await_args.kwargs["json"] == {"message_text": "Проверьте подписку"}

    async def test_truncates_text_the_api_would_reject(self) -> None:
        client, http_client = _client()
        await client.reply(17, "я" * (MAX_REPLY_LENGTH + 500))
        sent = http_client.post.await_args.kwargs["json"]["message_text"]
        assert len(sent) == MAX_REPLY_LENGTH

    async def test_reports_failure_on_an_error_status(self) -> None:
        client, _ = _client(post=AsyncMock(return_value=_response(400, {})))
        assert await client.reply(17, "текст") is False

    async def test_reports_failure_when_the_panel_is_unreachable(self) -> None:
        client, _ = _client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.reply(17, "текст") is False


class TestSetPriority:
    """Raising the priority is best effort — a failure must not lose the answer."""

    async def test_posts_the_priority(self) -> None:
        client, http_client = _client(post=AsyncMock(return_value=_response(200, {})))
        assert await client.set_priority(17, "high") is True
        assert http_client.post.await_args.args[0] == "http://bedolaga:8080/tickets/17/priority"
        assert http_client.post.await_args.kwargs["json"] == {"priority": "high"}

    async def test_returns_false_instead_of_raising(self) -> None:
        client, _ = _client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))
        assert await client.set_priority(17, "high") is False


class TestResolveTelegramId:
    """A ticket carries the panel's own user id, never a Telegram one."""

    async def test_reads_the_telegram_id_of_the_panel_user(self) -> None:
        client, http_client = _client(
            get=AsyncMock(return_value=_response(200, {"telegram_id": 42}))
        )
        assert await client.resolve_telegram_id(55) == 42
        assert http_client.get.await_args.args[0] == "http://bedolaga:8080/users/55"

    async def test_returns_none_for_a_cabinet_only_user(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"telegram_id": None})))
        assert await client.resolve_telegram_id(55) is None

    async def test_caches_the_lookup(self) -> None:
        get = AsyncMock(return_value=_response(200, {"telegram_id": 42}))
        client, _ = _client(get=get)
        await client.resolve_telegram_id(55)
        await client.resolve_telegram_id(55)
        assert get.await_count == 1

    async def test_does_not_cache_a_failed_lookup(self) -> None:
        get = AsyncMock(side_effect=[_response(500, {}), _response(200, {"telegram_id": 42})])
        client, _ = _client(get=get)
        assert await client.resolve_telegram_id(55) is None
        assert await client.resolve_telegram_id(55) == 42
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_client.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bedolaga.client'`

- [ ] **Step 3: Написать клиент**

Создать `app/bedolaga/client.py`:

```python
"""The only code in this bot that talks to the Bedolaga Web API."""

import logging

import httpx

from app.bedolaga.types import OPEN_STATUSES, Ticket, ticket_from_payload
from app.retry import post_with_retry

logger = logging.getLogger(__name__)

#: What `TicketReplyRequest.message_text` accepts; longer bodies are rejected.
MAX_REPLY_LENGTH: int = 4000

#: How many tickets one status query brings back per sweep.
DEFAULT_LIST_LIMIT: int = 50


class BedolagaClient:
    """Reads tickets from Bedolaga and answers them under a service API key."""

    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http_client = http_client
        # A panel user id never changes its Telegram id, and a busy ticket asks
        # for the same one on every turn.
        self._telegram_ids: dict[int, int] = {}

    @property
    def headers(self) -> dict[str, str]:
        """The service-token header every Web API endpoint requires."""
        return {"X-API-Key": self.api_key}

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        """Read one ticket with its messages, or None when it cannot be read.

        Events carry a truncated preview at best, so every answer starts here.
        """
        try:
            response = await self.http_client.get(
                f"{self.base_url}/tickets/{ticket_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read ticket %d: %s", ticket_id, e)
            return None

        if response.status_code != 200:
            logger.warning(
                "Bedolaga: reading ticket %d returned %d", ticket_id, response.status_code
            )
            return None

        try:
            return ticket_from_payload(response.json())
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Bedolaga: ticket %d came back malformed: %s", ticket_id, e)
            return None

    async def list_awaiting_ticket_ids(self, limit: int = DEFAULT_LIST_LIMIT) -> list[int]:
        """Ids of tickets in a status that means somebody is still waiting.

        The list endpoint serialises tickets without their messages, so it can
        only ever answer "which ones" — the caller reads each one in full.
        """
        ids: list[int] = []
        for status in sorted(OPEN_STATUSES):
            try:
                response = await self.http_client.get(
                    f"{self.base_url}/tickets",
                    headers=self.headers,
                    params={"status": status, "limit": limit},
                )
            except httpx.HTTPError as e:
                logger.warning("Bedolaga: could not list %s tickets: %s", status, e)
                continue

            if response.status_code != 200:
                logger.warning(
                    "Bedolaga: listing %s tickets returned %d", status, response.status_code
                )
                continue

            for item in response.json() or []:
                ticket_id = item.get("id")
                if ticket_id is not None:
                    ids.append(int(ticket_id))
        return ids

    async def reply(self, ticket_id: int, text: str) -> bool:
        """Post an answer into the ticket. True when Bedolaga accepted it.

        The panel takes it from here: the message is stored as an admin reply,
        the ticket flips to `answered`, and the user gets a Telegram
        notification plus a live update in the cabinet.
        """
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/tickets/{ticket_id}/reply",
                headers=self.headers,
                json={"message_text": text[:MAX_REPLY_LENGTH]},
                description=f"bedolaga reply to ticket {ticket_id}",
            )
        except httpx.HTTPError as e:
            logger.error("Bedolaga: replying to ticket %d failed: %s", ticket_id, e)
            return False

        if response.status_code not in (200, 201):
            logger.error(
                "Bedolaga: replying to ticket %d returned %d: %s",
                ticket_id,
                response.status_code,
                response.text[:200],
            )
            return False
        return True

    async def set_priority(self, ticket_id: int, priority: str) -> bool:
        """Raise or lower a ticket's priority. Best effort: never raises."""
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/tickets/{ticket_id}/priority",
                headers=self.headers,
                json={"priority": priority},
                description=f"bedolaga priority for ticket {ticket_id}",
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: setting priority on ticket %d failed: %s", ticket_id, e)
            return False
        return response.status_code == 200

    async def resolve_telegram_id(self, user_id: int) -> int | None:
        """The Telegram id behind a panel user id, or None when there is none.

        Cabinet accounts created by email or OAuth have no Telegram id at all,
        which is a normal answer here rather than a failure.
        """
        cached = self._telegram_ids.get(user_id)
        if cached is not None:
            return cached

        try:
            response = await self.http_client.get(
                f"{self.base_url}/users/{user_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read user %d: %s", user_id, e)
            return None

        if response.status_code != 200:
            logger.warning("Bedolaga: reading user %d returned %d", user_id, response.status_code)
            return None

        telegram_id = (response.json() or {}).get("telegram_id")
        if telegram_id is None:
            return None

        resolved = int(telegram_id)
        self._telegram_ids[user_id] = resolved
        return resolved
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_client.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/bedolaga/client.py tests/test_bedolaga_client.py && git commit -m "feat(bedolaga): API client for reading and answering tickets"
```

---

### Task 4: Состояние «что уже отвечено»

**Files:**
- Modify: `app/storage/models.py` (новая модель в конец файла)
- Create: `app/bedolaga/state.py`
- Test: `tests/test_bedolaga_state.py`

**Interfaces:**
- Consumes: `app.storage.database.Base`, `app.storage.database.DatabaseSessionManager`.
- Produces: модель `BedolagaTicketState` (таблица `bedolaga_ticket_state`); `TicketStateStore(db_manager)` с `async already_answered(ticket_id: int, message_id: int) -> bool` и `async mark_answered(ticket_id: int, message_id: int) -> None`.

Таблица создаётся автоматически: `db_manager.init_models()` в `main.py` вызывает `Base.metadata.create_all`, а модель импортируется через `app.bedolaga.state`. Миграция не нужна.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_state.py`:

```python
"""Unit tests for the ticket answering bookkeeping."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.state import TicketStateStore
from app.storage.models import BedolagaTicketState


class _FakeDbManager:
    """A session manager whose session is one mock everybody can inspect."""

    def __init__(self, row: Any = None) -> None:
        self.session_obj = MagicMock()
        self.session_obj.get = AsyncMock(return_value=row)
        self.session_obj.merge = AsyncMock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield self.session_obj


class TestAlreadyAnswered:
    """The same message must never be answered twice."""

    async def test_false_when_the_ticket_is_unknown(self) -> None:
        db = _FakeDbManager(row=None)
        assert await TicketStateStore(db).already_answered(17, 100) is False

    async def test_true_when_that_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        assert await TicketStateStore(db).already_answered(17, 100) is True

    async def test_true_when_a_later_message_was_answered(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=120))
        assert await TicketStateStore(db).already_answered(17, 100) is True

    async def test_false_for_a_message_newer_than_the_last_answer(self) -> None:
        db = _FakeDbManager(row=BedolagaTicketState(ticket_id=17, last_answered_message_id=100))
        assert await TicketStateStore(db).already_answered(17, 101) is False


class TestMarkAnswered:
    """Recording an answer upserts a single row per ticket."""

    async def test_merges_the_row(self) -> None:
        db = _FakeDbManager()
        await TicketStateStore(db).mark_answered(17, 101)
        merged = db.session_obj.merge.await_args.args[0]
        assert isinstance(merged, BedolagaTicketState)
        assert merged.ticket_id == 17
        assert merged.last_answered_message_id == 101
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_state.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'BedolagaTicketState'`

- [ ] **Step 3: Добавить модель**

В конец `app/storage/models.py`:

```python
class BedolagaTicketState(Base):
    """The last Bedolaga ticket message this bot has answered.

    Both the webhook and the reconciling poll can bring the same ticket in, and
    a delivery may arrive twice — this row is what makes answering a ticket
    idempotent instead of posting the same reply again.
    """

    __tablename__ = "bedolaga_ticket_state"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_answered_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<BedolagaTicketState ticket_id={self.ticket_id} "
            f"last_answered_message_id={self.last_answered_message_id}>"
        )
```

Создать `app/bedolaga/state.py`:

```python
"""Which ticket messages this bot has already answered."""

import logging
from datetime import UTC, datetime

from app.storage.database import DatabaseSessionManager
from app.storage.models import BedolagaTicketState

logger = logging.getLogger(__name__)


class TicketStateStore:
    """Reads and writes the one row per ticket that makes answering idempotent."""

    def __init__(self, db_manager: DatabaseSessionManager) -> None:
        self.db_manager = db_manager

    async def already_answered(self, ticket_id: int, message_id: int) -> bool:
        """True when this message, or a later one, has already been answered."""
        async with self.db_manager.session() as session:
            row = await session.get(BedolagaTicketState, ticket_id)
        return row is not None and row.last_answered_message_id >= message_id

    async def mark_answered(self, ticket_id: int, message_id: int) -> None:
        """Record that the ticket has been answered up to this message."""
        async with self.db_manager.session() as session:
            await session.merge(
                BedolagaTicketState(
                    ticket_id=ticket_id,
                    last_answered_message_id=message_id,
                    updated_at=datetime.now(UTC),
                )
            )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_state.py tests/test_storage_models.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/storage/models.py app/bedolaga/state.py tests/test_bedolaga_state.py && git commit -m "feat(bedolaga): remember which ticket message was answered last"
```

---

### Task 5: Ответ на тикет — основной путь

**Files:**
- Create: `app/bedolaga/pipeline.py`
- Modify: `app/constants.py` (новые ключи в `MESSAGES`)
- Test: `tests/test_bedolaga_pipeline.py`

**Interfaces:**
- Consumes: `BedolagaClient`, `TicketStateStore`, `app.llm.base.LlmClient` (`chat(user_message: str, telegram_user_id: int) -> LlmReply`), `app.llm.escalation.EscalationPolicy`, `app.bot.rate_limiter.UserRateLimiter`, `app.bot.admin_notifier.AdminNotifier`, `app.bot.keyed_lock.KeyedLock`, `app.constants.get_message`.
- Produces: `TicketUser(id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None)`; `TicketAnswerer(client, llm_client, state, rate_limiter, admin_notifier, forwarder, knowledge_gap_service, conversation_state)` с `schedule(ticket_id: int) -> None`, `async handle(ticket_id: int) -> None`, `async drain() -> None`.

В этой задаче конструктор принимает все восемь зависимостей, но `forwarder`, `knowledge_gap_service` и `conversation_state` пока только сохраняются — их поведение приезжает в Task 6.

- [ ] **Step 1: Добавить сообщения**

В `app/constants.py`, в словарь `MESSAGES`, после блока команд оператора:

```python
    # Bedolaga tickets
    "bedolaga.llm.empty": "Передаю обращение живому оператору — он ответит в этом тикете.",
    "bedolaga.escalation.note": (
        "\n\n———\nПередаю обращение живому оператору — он ответит в этом тикете."
    ),
    "bedolaga.mirror": "🎫 Тикет #{0} · {1}\n\nВопрос:\n{2}\n\nОтвет бота:\n{3}",
    "bedolaga.suppressed": "🎫 Тикет #{0} · {1}\n\nВопрос:\n{2}\n\n(бот молчит: с пользователем работает оператор)",
    "bedolaga.error.context": "Не удалось обработать тикет Bedolaga #{0}",
    "bedolaga.reply.failed": "Не удалось отправить ответ в тикет Bedolaga #{0}",
```

В `tests/test_constants.py`, в список `required_keys` теста `test_messages_dictionary_completeness`, дописать `"bedolaga.llm.empty"`, `"bedolaga.escalation.note"`, `"bedolaga.mirror"`.

Run: `.venv/bin/python -m pytest tests/test_constants.py -v --no-cov`
Expected: PASS

- [ ] **Step 2: Написать падающий тест**

Создать `tests/test_bedolaga_pipeline.py`:

```python
"""Unit tests for answering a Bedolaga ticket."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.types import Ticket, TicketMessage
from app.bot.conversation_state import ConversationState
from app.bot.rate_limiter import UserRateLimiter
from app.llm.base import LlmReply

TICKET_ID = 17
PANEL_USER_ID = 55
TELEGRAM_ID = 42


def _ticket(
    *messages: TicketMessage,
    status: str = "open",
    title: str = "Не подключается",
) -> Ticket:
    return Ticket(
        id=TICKET_ID,
        user_id=PANEL_USER_ID,
        title=title,
        status=status,
        messages=messages or (TicketMessage(id=100, text="Помогите", is_from_admin=False),),
    )


def _answerer(
    ticket: Ticket | None = None,
    reply: LlmReply | None = None,
    already_answered: bool = False,
    telegram_id: int | None = TELEGRAM_ID,
    reply_ok: bool = True,
    conversation_state: ConversationState | None = None,
) -> tuple[TicketAnswerer, dict[str, Any]]:
    client = MagicMock()
    client.get_ticket = AsyncMock(return_value=ticket if ticket is not None else _ticket())
    client.resolve_telegram_id = AsyncMock(return_value=telegram_id)
    client.reply = AsyncMock(return_value=reply_ok)
    client.set_priority = AsyncMock(return_value=True)

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(
        return_value=reply if reply is not None else LlmReply(text="Проверьте подписку")
    )

    state = MagicMock()
    state.already_answered = AsyncMock(return_value=already_answered)
    state.mark_answered = AsyncMock()

    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()

    admin_notifier = MagicMock()
    admin_notifier.notify_error = AsyncMock()

    knowledge_gap_service = MagicMock()
    knowledge_gap_service.evaluate = AsyncMock()

    answerer = TicketAnswerer(
        client=client,
        llm_client=llm_client,
        state=state,
        rate_limiter=UserRateLimiter(),
        admin_notifier=admin_notifier,
        forwarder=forwarder,
        knowledge_gap_service=knowledge_gap_service,
        conversation_state=conversation_state or ConversationState(),
    )
    parts = {
        "client": client,
        "llm_client": llm_client,
        "state": state,
        "forwarder": forwarder,
        "admin_notifier": admin_notifier,
        "knowledge_gap_service": knowledge_gap_service,
    }
    return answerer, parts


class TestAnswering:
    """The happy path: read the ticket, ask the model, write the answer back."""

    async def test_asks_the_model_under_the_telegram_id_of_the_author(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        question, user_id = parts["llm_client"].chat.await_args.args
        assert question == "Не подключается\n\nПомогите"
        assert user_id == TELEGRAM_ID

    async def test_posts_the_answer_into_the_ticket(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_awaited_once_with(TICKET_ID, "Проверьте подписку")

    async def test_records_the_answered_message(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_awaited_once_with(TICKET_ID, 100)

    async def test_strips_the_escalation_marker_from_the_answer(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Держите [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        assert parts["client"].reply.await_args.args[1].startswith("Держите")
        assert "[ESCALATE]" not in parts["client"].reply.await_args.args[1]

    async def test_falls_back_to_a_handover_line_when_the_model_says_nothing(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="   "))
        await answerer.handle(TICKET_ID)
        assert "оператору" in parts["client"].reply.await_args.args[1]


class TestSkipping:
    """Everything that must not produce a reply."""

    async def test_ignores_a_ticket_that_cannot_be_read(self) -> None:
        answerer, parts = _answerer()
        parts["client"].get_ticket = AsyncMock(return_value=None)
        answerer.client = parts["client"]
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_ticket_whose_last_message_is_ours(self) -> None:
        ticket = _ticket(
            TicketMessage(id=100, text="Помогите", is_from_admin=False),
            TicketMessage(id=101, text="Проверьте подписку", is_from_admin=True),
            status="answered",
        )
        answerer, parts = _answerer(ticket=ticket)
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_closed_ticket(self) -> None:
        answerer, parts = _answerer(ticket=_ticket(status="closed"))
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()

    async def test_ignores_a_message_already_answered(self) -> None:
        answerer, parts = _answerer(already_answered=True)
        await answerer.handle(TICKET_ID)
        parts["llm_client"].chat.assert_not_awaited()

    async def test_does_not_record_an_answer_the_panel_rejected(self) -> None:
        answerer, parts = _answerer(reply_ok=False)
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_not_awaited()
        parts["admin_notifier"].notify_error.assert_awaited()

    async def test_a_rate_limited_user_is_left_for_the_next_sweep(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.reset_mock()
        parts["state"].mark_answered.reset_mock()
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()
        parts["state"].mark_answered.assert_not_awaited()


class TestCabinetOnlyUsers:
    """A cabinet account without a Telegram id still gets an answer."""

    async def test_uses_a_synthetic_negative_key(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        _, user_id = parts["llm_client"].chat.await_args.args
        assert user_id == -PANEL_USER_ID

    async def test_still_answers_the_ticket(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_awaited_once()


class TestFailureHandling:
    """A model failure is reported, never silently swallowed."""

    async def test_notifies_admins_when_the_turn_raises(self) -> None:
        answerer, parts = _answerer()
        parts["llm_client"].chat = AsyncMock(side_effect=RuntimeError("boom"))
        answerer.llm_client = parts["llm_client"]
        await answerer.handle(TICKET_ID)
        parts["admin_notifier"].notify_error.assert_awaited()
        parts["state"].mark_answered.assert_not_awaited()


class TestScheduling:
    """Webhook delivery must return at once, so the work runs in the background."""

    async def test_schedule_runs_the_turn_and_drain_waits_for_it(self) -> None:
        answerer, parts = _answerer()
        answerer.schedule(TICKET_ID)
        await answerer.drain()
        parts["client"].reply.assert_awaited_once()

    async def test_drain_is_a_no_op_without_work(self) -> None:
        answerer, _ = _answerer()
        await answerer.drain()
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bedolaga.pipeline'`

- [ ] **Step 4: Написать пайплайн**

Создать `app/bedolaga/pipeline.py`:

```python
"""One turn of a Bedolaga ticket conversation: read it, answer it, write it back."""

import asyncio
import logging
from dataclasses import dataclass

from app.bedolaga.client import BedolagaClient
from app.bedolaga.state import TicketStateStore
from app.bedolaga.types import Ticket
from app.bot.admin_notifier import AdminNotifier
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.keyed_lock import KeyedLock
from app.bot.rate_limiter import UserRateLimiter
from app.constants import get_message
from app.llm.base import LlmClient
from app.llm.escalation import EscalationPolicy
from app.rag.knowledge_gaps import KnowledgeGapService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketUser:
    """A stand-in for an aiogram user, for the code that forwards to a topic.

    SupportGroupForwarder only ever reads `id`, `username`, `first_name` and
    `last_name` off the sender, and a ticket has no aiogram update behind it.
    """

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TicketAnswerer:
    """Answers Bedolaga tickets with the model that answers Telegram messages."""

    def __init__(
        self,
        client: BedolagaClient,
        llm_client: LlmClient,
        state: TicketStateStore,
        rate_limiter: UserRateLimiter,
        admin_notifier: AdminNotifier,
        forwarder: SupportGroupForwarder,
        knowledge_gap_service: KnowledgeGapService,
        conversation_state: ConversationState,
    ) -> None:
        self.client = client
        self.llm_client = llm_client
        self.state = state
        self.rate_limiter = rate_limiter
        self.admin_notifier = admin_notifier
        self.forwarder = forwarder
        self.knowledge_gap_service = knowledge_gap_service
        self.conversation_state = conversation_state
        # One turn per ticket: a webhook and a poll sweep can both bring in the
        # same ticket a millisecond apart.
        self._tickets = KeyedLock()
        self._in_flight: set[asyncio.Task[None]] = set()

    def schedule(self, ticket_id: int) -> None:
        """Answer this ticket in the background.

        A webhook delivery has ten seconds before Bedolaga gives up on it, and
        a model turn takes longer than that — so the HTTP handler schedules and
        answers 200 immediately.
        """
        task = asyncio.create_task(self.handle(ticket_id), name=f"bedolaga-ticket-{ticket_id}")
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def drain(self) -> None:
        """Wait for the turns already in flight — used on shutdown."""
        if self._in_flight:
            await asyncio.gather(*tuple(self._in_flight), return_exceptions=True)

    async def handle(self, ticket_id: int) -> None:
        """Answer one ticket, one turn at a time, never raising to the caller."""
        async with self._tickets.hold(ticket_id):
            try:
                await self._answer(ticket_id)
            except Exception as e:
                logger.error("Failed to answer Bedolaga ticket %d: %s", ticket_id, e, exc_info=True)
                await self.admin_notifier.notify_error(
                    get_message("bedolaga.error.context", ticket_id),
                    error=e,
                )

    async def _answer(self, ticket_id: int) -> None:
        ticket = await self.client.get_ticket(ticket_id)
        if ticket is None or not ticket.awaits_answer:
            return

        last = ticket.last_message
        if last is None or await self.state.already_answered(ticket.id, last.id):
            return

        user_key = await self.user_key(ticket)
        if not self.rate_limiter.try_acquire(user_key):
            # Nothing is recorded, so the next sweep answers this message once
            # the window has passed.
            logger.info("Bedolaga ticket %d is rate limited for user %d", ticket.id, user_key)
            return

        question = ticket.question
        reply = await self.llm_client.chat(question, user_key)
        answer = EscalationPolicy.strip_marker(reply.text) or get_message("bedolaga.llm.empty")

        if not await self.client.reply(ticket.id, answer):
            await self.admin_notifier.notify_error(
                get_message("bedolaga.reply.failed", ticket.id),
                user_id=user_key,
            )
            return

        await self.state.mark_answered(ticket.id, last.id)

    async def user_key(self, ticket: Ticket) -> int:
        """The id this ticket's conversation is kept under.

        A Telegram id is what the rest of the bot keys on — chat history, FAQ
        follow-ups and every Remnawave lookup. A cabinet account registered by
        email has none, so it gets its panel id with the sign flipped: unique
        per person, never colliding with a real Telegram id, and finding
        nothing in Remnawave, which is exactly right — we cannot prove who
        that person is.
        """
        telegram_id = await self.client.resolve_telegram_id(ticket.user_id)
        return telegram_id if telegram_id else -ticket.user_id
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py -v --no-cov`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add app/bedolaga/pipeline.py app/constants.py tests/test_bedolaga_pipeline.py tests/test_constants.py && git commit -m "feat(bedolaga): answer tickets with the support model"
```

---

### Task 6: Эскалация, зеркало в топик и учёт пробелов

**Files:**
- Modify: `app/bedolaga/pipeline.py` (`_answer`, плюс новые методы)
- Test: `tests/test_bedolaga_pipeline.py` (новые классы тестов)

**Interfaces:**
- Consumes: `SupportGroupForwarder.forward_to_support(user_chat_id: int, user_message_ids: Sequence[int] | None, user: Any, bot_response: str, needs_escalation: bool, illustration_message_id: int | None = None)`; `KnowledgeGapService.evaluate(user_query: str, telegram_user_id: int, raw_bot_response: str | None, faq_context: FaqContext | None)`; `ConversationState.is_operator_recently_active(user_id) -> bool`, `record_query(user_id, query, faq_context)`; `BedolagaClient.set_priority`.
- Produces: приватные методы `TicketAnswerer.mirror(...)`, `TicketAnswerer.stand_in(...)` — наружу ничего нового.

Поведение, которое добавляется:
1. Если с пользователем прямо сейчас работает оператор (`is_operator_recently_active`) — бот **не** отвечает в тикет, а кладёт вопрос в топик с пометкой.
2. Если модель попросила человека (`[ESCALATE]`) или пользователь сам его позвал — к ответу добавляется строка про оператора, приоритет тикета поднимается до `high`, а в группу уходит алерт.
3. Каждый обработанный тикет зеркалится в форум-топик пользователя: вопрос + ответ одним сообщением.
4. Вопрос уходит в учёт пробелов знаний и в `ConversationState` — ровно как в Telegram-пайплайне.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/test_bedolaga_pipeline.py`:

```python
class TestEscalation:
    """A ticket the model cannot close is handed to a human, loudly."""

    async def test_appends_a_handover_line_when_the_model_asks(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        sent = parts["client"].reply.await_args.args[1]
        assert sent.startswith("Не могу помочь")
        assert "оператор" in sent.lower()

    async def test_raises_the_ticket_priority(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_awaited_once_with(TICKET_ID, "high")

    async def test_escalates_when_the_user_asks_for_a_human(self) -> None:
        ticket = _ticket(
            TicketMessage(id=100, text="Хочу поговорить с оператором", is_from_admin=False)
        )
        answerer, parts = _answerer(ticket=ticket)
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_awaited_once_with(TICKET_ID, "high")

    async def test_tags_the_mirrored_message_for_escalation(self) -> None:
        answerer, parts = _answerer(reply=LlmReply(text="Не могу помочь [ESCALATE]"))
        await answerer.handle(TICKET_ID)
        assert parts["forwarder"].forward_to_support.await_args.kwargs["needs_escalation"] is True

    async def test_leaves_priority_alone_on_an_ordinary_answer(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        parts["client"].set_priority.assert_not_awaited()


class TestMirroring:
    """Operators read the support group, so the ticket turn shows up there too."""

    async def test_mirrors_question_and_answer_into_the_topic(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        kwargs = parts["forwarder"].forward_to_support.await_args.kwargs
        assert kwargs["user_chat_id"] == TELEGRAM_ID
        assert kwargs["user_message_ids"] is None
        assert "Помогите" in kwargs["bot_response"]
        assert "Проверьте подписку" in kwargs["bot_response"]
        assert str(TICKET_ID) in kwargs["bot_response"]

    async def test_names_a_cabinet_only_user_in_the_topic_title(self) -> None:
        answerer, parts = _answerer(telegram_id=None)
        await answerer.handle(TICKET_ID)
        user = parts["forwarder"].forward_to_support.await_args.kwargs["user"]
        assert user.id == -PANEL_USER_ID
        assert str(PANEL_USER_ID) in (user.first_name or "")

    async def test_a_failing_mirror_does_not_lose_the_answer(self) -> None:
        answerer, parts = _answerer()
        parts["forwarder"].forward_to_support = AsyncMock(side_effect=RuntimeError("no topic"))
        answerer.forwarder = parts["forwarder"]
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_awaited_once_with(TICKET_ID, 100)


class TestOperatorSuppression:
    """While a human is holding the conversation, the bot stays out of it."""

    async def test_does_not_answer_the_ticket(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        parts["client"].reply.assert_not_awaited()
        parts["llm_client"].chat.assert_not_awaited()

    async def test_puts_the_question_in_the_topic_instead(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        text = parts["forwarder"].forward_to_support.await_args.kwargs["bot_response"]
        assert "Помогите" in text

    async def test_does_not_mark_the_message_answered(self) -> None:
        conversation_state = ConversationState()
        conversation_state.record_operator_reply(TELEGRAM_ID)
        answerer, parts = _answerer(conversation_state=conversation_state)
        await answerer.handle(TICKET_ID)
        parts["state"].mark_answered.assert_not_awaited()


class TestKnowledgeGaps:
    """A ticket nobody could answer is a gap in the FAQ, same as a chat message."""

    async def test_evaluates_the_question(self) -> None:
        answerer, parts = _answerer()
        await answerer.handle(TICKET_ID)
        query, user_id, raw_response, _ = parts["knowledge_gap_service"].evaluate.await_args.args
        assert "Помогите" in query
        assert user_id == TELEGRAM_ID
        assert raw_response == "Проверьте подписку"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py -v --no-cov`
Expected: FAIL — новые классы падают (`forward_to_support` не вызывался, `set_priority` не вызывался и т.д.)

- [ ] **Step 3: Дописать пайплайн**

Заменить метод `_answer` в `app/bedolaga/pipeline.py` целиком на:

```python
    async def _answer(self, ticket_id: int) -> None:
        ticket = await self.client.get_ticket(ticket_id)
        if ticket is None or not ticket.awaits_answer:
            return

        last = ticket.last_message
        if last is None or await self.state.already_answered(ticket.id, last.id):
            return

        user_key = await self.user_key(ticket)
        question = ticket.question

        if self.conversation_state.is_operator_recently_active(user_key):
            # The operator is holding this conversation in Telegram; a bot
            # answer in the ticket would talk over them.
            await self.mirror(
                ticket,
                user_key,
                get_message("bedolaga.suppressed", ticket.id, ticket.title, question),
                escalate=True,
            )
            return

        if not self.rate_limiter.try_acquire(user_key):
            # Nothing is recorded, so the next sweep answers this message once
            # the window has passed.
            logger.info("Bedolaga ticket %d is rate limited for user %d", ticket.id, user_key)
            return

        reply = await self.llm_client.chat(question, user_key)
        answer = EscalationPolicy.strip_marker(reply.text) or get_message("bedolaga.llm.empty")
        escalate = EscalationPolicy.model_requested_escalation(
            reply.text
        ) or EscalationPolicy.user_requests_human(question)

        posted = answer + get_message("bedolaga.escalation.note") if escalate else answer
        if not await self.client.reply(ticket.id, posted):
            await self.admin_notifier.notify_error(
                get_message("bedolaga.reply.failed", ticket.id),
                user_id=user_key,
            )
            return

        await self.state.mark_answered(ticket.id, last.id)
        self.conversation_state.record_query(user_key, question, reply.faq_context)

        if escalate:
            await self.client.set_priority(ticket.id, "high")

        await self.mirror(
            ticket,
            user_key,
            get_message("bedolaga.mirror", ticket.id, ticket.title, question, answer),
            escalate=escalate,
        )

        if question.strip():
            await self.knowledge_gap_service.evaluate(
                question,
                user_key,
                reply.text,
                reply.faq_context,
            )
```

Добавить в класс два метода:

```python
async def mirror(self, ticket: Ticket, user_key: int, text: str, escalate: bool) -> None:
    """Put this ticket turn into the user's forum topic.

    The answer is already delivered — by Bedolaga, into the ticket — so a
    support group that is down or misconfigured must not cost the user
    their reply. Every failure here stays here.
    """
    try:
        await self.forwarder.forward_to_support(
            user_chat_id=user_key,
            user_message_ids=None,
            user=self.stand_in(ticket, user_key),
            bot_response=text,
            needs_escalation=escalate,
        )
    except Exception as e:
        logger.warning("Could not mirror Bedolaga ticket %d to the topic: %s", ticket.id, e)


@staticmethod
def stand_in(ticket: Ticket, user_key: int) -> TicketUser:
    """The sender the forwarder needs to find or name a topic.

    A Telegram user already has a topic under their own id. A cabinet-only
    account does not, so its topic is named after the panel account rather
    than the synthetic negative id nobody would recognise.
    """
    if user_key > 0:
        return TicketUser(id=user_key)
    return TicketUser(id=user_key, first_name=f"Кабинет #{ticket.user_id}")
```

- [ ] **Step 4: Убедиться, что все тесты файла проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/bedolaga/pipeline.py tests/test_bedolaga_pipeline.py && git commit -m "feat(bedolaga): escalate, mirror to the topic and record knowledge gaps"
```

---

### Task 7: Приём вебхука

**Files:**
- Create: `app/bedolaga/webhook.py`
- Test: `tests/test_bedolaga_webhook.py`

**Interfaces:**
- Consumes: `TicketAnswerer.schedule(ticket_id: int)`, `aiohttp.web`.
- Produces: `signature_matches(secret: str, body: bytes, header: str | None) -> bool`; `BedolagaWebhookEndpoint(answerer: TicketAnswerer, secret: str = "")` с `HANDLED_EVENTS: frozenset[str]`, `register(app: web.Application, path: str) -> None`, `async handle(request: web.Request) -> web.Response`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_webhook.py`:

```python
"""Unit tests for the Bedolaga webhook endpoint."""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from app.bedolaga.webhook import BedolagaWebhookEndpoint, signature_matches

SECRET = "webhook-secret"


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _request(body: bytes, event: str, signature: str | None = None) -> Any:
    request = MagicMock()
    request.read = AsyncMock(return_value=body)
    headers = {"X-Webhook-Event": event}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature
    request.headers = headers
    return request


def _endpoint(secret: str = SECRET) -> tuple[BedolagaWebhookEndpoint, MagicMock]:
    answerer = MagicMock()
    answerer.schedule = MagicMock()
    return BedolagaWebhookEndpoint(answerer=answerer, secret=secret), answerer


class TestSignatureMatches:
    """The endpoint is reachable from outside; the secret is what guards it."""

    def test_accepts_a_correct_signature(self) -> None:
        body = _body({"ticket_id": 17})
        assert signature_matches(SECRET, body, _signature(body)) is True

    def test_rejects_a_signature_from_another_secret(self) -> None:
        body = _body({"ticket_id": 17})
        assert signature_matches(SECRET, body, _signature(body, "other")) is False

    def test_rejects_a_missing_signature(self) -> None:
        assert signature_matches(SECRET, _body({}), None) is False

    def test_accepts_anything_when_no_secret_is_configured(self) -> None:
        assert signature_matches("", _body({}), None) is True


class TestHandle:
    """What each kind of delivery does."""

    async def test_schedules_the_ticket_on_a_new_ticket(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "user_id": 55, "title": "t"})
        response = await endpoint.handle(_request(body, "ticket.created", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_called_once_with(17)

    async def test_schedules_the_ticket_on_a_user_message(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "message_id": 101, "is_from_admin": False})
        response = await endpoint.handle(_request(body, "ticket.message_added", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_called_once_with(17)

    async def test_ignores_our_own_reply_coming_back(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "message_id": 102, "is_from_admin": True})
        response = await endpoint.handle(_request(body, "ticket.message_added", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_not_called()

    async def test_ignores_an_unrelated_event(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17, "new_status": "closed"})
        response = await endpoint.handle(_request(body, "ticket.status_changed", _signature(body)))
        assert response.status == 200
        answerer.schedule.assert_not_called()

    async def test_rejects_a_bad_signature(self) -> None:
        endpoint, answerer = _endpoint()
        body = _body({"ticket_id": 17})
        response = await endpoint.handle(
            _request(body, "ticket.created", _signature(body, "other"))
        )
        assert response.status == 403
        answerer.schedule.assert_not_called()

    async def test_rejects_a_body_that_is_not_json(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(b"not json", "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()

    async def test_rejects_a_payload_without_a_ticket_id(self) -> None:
        endpoint, answerer = _endpoint(secret="")
        response = await endpoint.handle(_request(_body({"user_id": 55}), "ticket.created"))
        assert response.status == 400
        answerer.schedule.assert_not_called()


class TestRegister:
    """The endpoint hangs off the healthcheck server the bot already runs."""

    def test_adds_a_post_route(self) -> None:
        endpoint, _ = _endpoint()
        app = web.Application()
        endpoint.register(app, "/bedolaga/webhook")
        routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
        assert ("POST", "/bedolaga/webhook") in routes
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_webhook.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bedolaga.webhook'`

- [ ] **Step 3: Написать эндпоинт**

Создать `app/bedolaga/webhook.py`:

```python
"""Receives Bedolaga ticket events on the bot's own HTTP server."""

import hashlib
import hmac
import json
import logging

from aiohttp import web

from app.bedolaga.pipeline import TicketAnswerer

logger = logging.getLogger(__name__)


def signature_matches(secret: str, body: bytes, header: str | None) -> bool:
    """Verify the `X-Webhook-Signature` Bedolaga sends over the raw body.

    With no secret configured there is nothing to check — the endpoint is then
    only as safe as the network it listens on, which is why the README tells
    you to set one.
    """
    if not secret:
        return True
    if not header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    supplied = header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, supplied)


class BedolagaWebhookEndpoint:
    """Turns a ticket event into scheduled work and answers immediately."""

    #: `ticket.status_changed` is delivered too, but a status change on its own
    #: never means somebody is waiting for an answer.
    HANDLED_EVENTS: frozenset[str] = frozenset({"ticket.created", "ticket.message_added"})

    def __init__(self, answerer: TicketAnswerer, secret: str = "") -> None:
        self.answerer = answerer
        self.secret = secret

    def register(self, app: web.Application, path: str) -> None:
        """Mount the endpoint on the aiohttp app that already serves /health."""
        app.router.add_post(path, self.handle)
        logger.info("Bedolaga webhook endpoint registered at %s", path)

    async def handle(self, request: web.Request) -> web.Response:
        """Accept one delivery. Never does the work inline.

        Bedolaga gives a webhook ten seconds and does not retry a failure, so
        this returns as soon as the event is understood; the answer happens on
        a background task.
        """
        body = await request.read()

        if not signature_matches(self.secret, body, request.headers.get("X-Webhook-Signature")):
            logger.warning("Bedolaga webhook: rejected a delivery with a bad signature")
            return web.json_response({"status": "forbidden"}, status=403)

        event = request.headers.get("X-Webhook-Event", "")
        if event not in self.HANDLED_EVENTS:
            return web.json_response({"status": "ignored"})

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError, UnicodeDecodeError:
            logger.warning("Bedolaga webhook: body was not JSON")
            return web.json_response({"status": "bad request"}, status=400)

        if payload.get("is_from_admin"):
            # Our own reply is stored as an admin message and comes straight
            # back as an event. Answering it would answer ourselves, forever.
            return web.json_response({"status": "ignored"})

        ticket_id = payload.get("ticket_id")
        if ticket_id is None:
            logger.warning("Bedolaga webhook: %s carried no ticket_id", event)
            return web.json_response({"status": "bad request"}, status=400)

        self.answerer.schedule(int(ticket_id))
        return web.json_response({"status": "accepted"})
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_webhook.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/bedolaga/webhook.py tests/test_bedolaga_webhook.py && git commit -m "feat(bedolaga): receive ticket webhooks on the healthcheck server"
```

---

### Task 8: Сверочный поллинг

**Files:**
- Create: `app/bedolaga/poller.py`
- Test: `tests/test_bedolaga_poller.py`

**Interfaces:**
- Consumes: `BedolagaClient.list_awaiting_ticket_ids(limit)`, `TicketAnswerer.schedule(ticket_id)`.
- Produces: `TicketPoller(client: BedolagaClient, answerer: TicketAnswerer, limit: int = 50)` с `async sweep() -> int`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_poller.py`:

```python
"""Unit tests for the reconciling sweep over open tickets."""

from unittest.mock import AsyncMock, MagicMock

from app.bedolaga.poller import TicketPoller


def _poller(ids: list[int] | None = None, error: Exception | None = None):
    client = MagicMock()
    client.list_awaiting_ticket_ids = AsyncMock(
        side_effect=error,
        return_value=ids if ids is not None else [17, 18],
    )
    answerer = MagicMock()
    answerer.schedule = MagicMock()
    return TicketPoller(client=client, answerer=answerer), client, answerer


class TestSweep:
    """A webhook Bedolaga failed to deliver is never retried — this is the net."""

    async def test_schedules_every_open_ticket(self) -> None:
        poller, _, answerer = _poller()
        assert await poller.sweep() == 2
        assert [call.args[0] for call in answerer.schedule.call_args_list] == [17, 18]

    async def test_schedules_nothing_when_no_ticket_is_waiting(self) -> None:
        poller, _, answerer = _poller(ids=[])
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()

    async def test_passes_its_limit_to_the_client(self) -> None:
        poller, client, _ = _poller()
        poller.limit = 10
        await poller.sweep()
        client.list_awaiting_ticket_ids.assert_awaited_once_with(10)

    async def test_a_failing_sweep_reports_zero_instead_of_raising(self) -> None:
        poller, _, answerer = _poller(error=RuntimeError("panel down"))
        assert await poller.sweep() == 0
        answerer.schedule.assert_not_called()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_poller.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bedolaga.poller'`

- [ ] **Step 3: Написать поллер**

Создать `app/bedolaga/poller.py`:

```python
"""The sweep that catches the ticket events a webhook never delivered."""

import logging

from app.bedolaga.client import DEFAULT_LIST_LIMIT, BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer

logger = logging.getLogger(__name__)


class TicketPoller:
    """Schedules every ticket that is still waiting for support.

    Bedolaga's webhook delivery has no retries: one timeout and that ticket
    would sit unanswered forever. Scheduling is cheap and idempotent — a ticket
    already answered is dropped by the answerer after a single read.
    """

    def __init__(
        self,
        client: BedolagaClient,
        answerer: TicketAnswerer,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> None:
        self.client = client
        self.answerer = answerer
        self.limit = limit

    async def sweep(self) -> int:
        """Schedule the open tickets. Returns how many were scheduled."""
        try:
            ticket_ids = await self.client.list_awaiting_ticket_ids(self.limit)
        except Exception as e:
            # The scheduler logs and retries on the next tick; nothing is lost.
            logger.warning("Bedolaga ticket sweep failed: %s", e)
            return 0

        for ticket_id in ticket_ids:
            self.answerer.schedule(ticket_id)

        if ticket_ids:
            logger.info("Bedolaga sweep scheduled %d ticket(s)", len(ticket_ids))
        return len(ticket_ids)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_poller.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/bedolaga/poller.py tests/test_bedolaga_poller.py && git commit -m "feat(bedolaga): reconcile open tickets on a schedule"
```

---

### Task 9: Проводка в приложение

**Files:**
- Modify: `app/bedolaga/__init__.py` (фабрика `create_ticket_support`)
- Modify: `app/main.py` (создание, регистрация маршрута, job обслуживания, drain при остановке)
- Test: `tests/test_bedolaga_support.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: всё из Task 3–8, `app.bot.maintenance.MaintenanceJob`, `app.config.Settings`, `app.config.reveal`.
- Produces: `TicketSupport` (dataclass с полями `answerer`, `poller`, `endpoint`, `webhook_path`, `poll_interval_seconds`) с `register_routes(app: web.Application) -> None` и `maintenance_job() -> MaintenanceJob`; функция `create_ticket_support(settings, http_client, llm_client, db_manager, forwarder, admin_notifier, rate_limiter, knowledge_gap_service, conversation_state) -> TicketSupport | None`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_bedolaga_support.py`:

```python
"""Unit tests for assembling the Bedolaga ticket integration."""

from unittest.mock import MagicMock

import httpx
from aiohttp import web

from app.bedolaga import create_ticket_support
from app.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "telegram_bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "telegram_support_group_chat_id": -1001234567890,
        "llm_provider": "deepseek",
        "deepseek_api_key": "sk-test",
        "deepseek_model": "deepseek-chat",
        "embedding_provider": "gemini",
        "gemini_api_key": "test",
        "gemini_model": "gemini-2.5-flash",
        "pgvector_password": "secret",
    }
    base.update(overrides)
    return Settings(**base)


def _create(settings: Settings):
    return create_ticket_support(
        settings=settings,
        http_client=MagicMock(spec=httpx.AsyncClient),
        llm_client=MagicMock(),
        db_manager=MagicMock(),
        forwarder=MagicMock(),
        admin_notifier=MagicMock(),
        rate_limiter=MagicMock(),
        knowledge_gap_service=MagicMock(),
        conversation_state=MagicMock(),
    )


class TestCreateTicketSupport:
    """Nothing is built, mounted or scheduled while the integration is off."""

    def test_returns_none_when_disabled(self) -> None:
        assert _create(_settings()) is None

    def test_builds_the_integration_when_enabled(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
            )
        )
        assert support is not None
        assert support.poller.client is support.answerer.client

    def test_mounts_the_webhook_route(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
                bedolaga_webhook_path="/hooks/tickets",
            )
        )
        assert support is not None
        app = web.Application()
        support.register_routes(app)
        routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
        assert ("POST", "/hooks/tickets") in routes

    def test_builds_a_maintenance_job_on_the_configured_interval(self) -> None:
        support = _create(
            _settings(
                bedolaga_enabled=True,
                bedolaga_api_url="http://bedolaga:8080",
                bedolaga_api_key="token",
                bedolaga_poll_interval_seconds=30,
            )
        )
        assert support is not None
        job = support.maintenance_job()
        assert job.interval_seconds == 30
        assert "bedolaga" in job.name
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_support.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'create_ticket_support' from 'app.bedolaga'`

- [ ] **Step 3: Написать фабрику**

Заменить содержимое `app/bedolaga/__init__.py` на:

```python
"""Answering Bedolaga support tickets with the same model that answers Telegram."""

import logging
from dataclasses import dataclass

import httpx
from aiohttp import web

from app.bedolaga.client import BedolagaClient
from app.bedolaga.pipeline import TicketAnswerer
from app.bedolaga.poller import TicketPoller
from app.bedolaga.state import TicketStateStore
from app.bedolaga.webhook import BedolagaWebhookEndpoint
from app.bot.admin_notifier import AdminNotifier
from app.bot.conversation_state import ConversationState
from app.bot.forwarder import SupportGroupForwarder
from app.bot.maintenance import MaintenanceJob
from app.bot.rate_limiter import UserRateLimiter
from app.config import Settings, reveal
from app.llm.base import LlmClient
from app.rag.knowledge_gaps import KnowledgeGapService
from app.storage.database import DatabaseSessionManager

logger = logging.getLogger(__name__)

__all__ = ["TicketSupport", "create_ticket_support"]


@dataclass(frozen=True)
class TicketSupport:
    """Everything the ticket integration needs the application to hold on to."""

    answerer: TicketAnswerer
    poller: TicketPoller
    endpoint: BedolagaWebhookEndpoint
    webhook_path: str
    poll_interval_seconds: float

    def register_routes(self, app: web.Application) -> None:
        """Mount the webhook endpoint on the bot's HTTP server."""
        self.endpoint.register(app, self.webhook_path)

    def maintenance_job(self) -> MaintenanceJob:
        """The recurring sweep, in the shape MaintenanceScheduler runs."""
        return MaintenanceJob(
            name="bedolaga-ticket-sweep",
            interval_seconds=self.poll_interval_seconds,
            run=self.poller.sweep,
        )


def create_ticket_support(
    settings: Settings,
    http_client: httpx.AsyncClient,
    llm_client: LlmClient,
    db_manager: DatabaseSessionManager,
    forwarder: SupportGroupForwarder,
    admin_notifier: AdminNotifier,
    rate_limiter: UserRateLimiter,
    knowledge_gap_service: KnowledgeGapService,
    conversation_state: ConversationState,
) -> TicketSupport | None:
    """Assemble the Bedolaga ticket integration, or None when it is switched off."""
    if not settings.bedolaga_enabled:
        return None

    client = BedolagaClient(
        base_url=settings.bedolaga_api_url,
        api_key=reveal(settings.bedolaga_api_key),
        http_client=http_client,
    )
    answerer = TicketAnswerer(
        client=client,
        llm_client=llm_client,
        state=TicketStateStore(db_manager),
        rate_limiter=rate_limiter,
        admin_notifier=admin_notifier,
        forwarder=forwarder,
        knowledge_gap_service=knowledge_gap_service,
        conversation_state=conversation_state,
    )
    logger.info(
        "Bedolaga ticket integration enabled: %s, sweeping every %ds",
        settings.bedolaga_api_url,
        settings.bedolaga_poll_interval_seconds,
    )
    return TicketSupport(
        answerer=answerer,
        poller=TicketPoller(client=client, answerer=answerer),
        endpoint=BedolagaWebhookEndpoint(
            answerer=answerer,
            secret=reveal(settings.bedolaga_webhook_secret),
        ),
        webhook_path=settings.bedolaga_webhook_path,
        poll_interval_seconds=float(settings.bedolaga_poll_interval_seconds),
    )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_support.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Подключить в `main.py`**

В `app/main.py`:

1. К импортам добавить `from app.bedolaga import TicketSupport, create_ticket_support`.
2. Рядом с остальными «поздними» переменными (`maintenance: MaintenanceScheduler | None = None`) добавить:

```python
    ticket_support: TicketSupport | None = None
```

3. После создания `pipeline` / `operator_ask` (шаг 5 композиции), до сборки роутера:

```python
        ticket_support = create_ticket_support(
            settings=settings,
            http_client=http_client,
            llm_client=llm_client,
            db_manager=db_manager,
            forwarder=forwarder,
            admin_notifier=admin_notifier,
            rate_limiter=rate_limiter,
            knowledge_gap_service=knowledge_gap_service,
            conversation_state=conversation_state,
        )
```

4. В шаге 7 (запуск обслуживания) заменить построение списка задач на:

```python
        jobs = build_default_jobs(chat_history_service, rate_limiter, conversation_state)
        if ticket_support is not None:
            jobs.append(ticket_support.maintenance_job())
        maintenance = MaintenanceScheduler(jobs)
        maintenance.start()
```

5. В шаге 8 (healthcheck-сервер), между `create_health_app()` и `start_health_server(...)`:

```python
        health_app = create_health_app()
        if ticket_support is not None:
            ticket_support.register_routes(health_app)
        health_runner = await start_health_server(
            health_app,
            port=settings.healthcheck_port,
        )
```

6. В блоке `finally`, сразу после остановки `maintenance` и до `message_buffer`:

```python
        if ticket_support is not None:
            # A ticket half-answered on shutdown is a user waiting forever:
            # the model call already cost tokens, and nothing would retry it.
            await ticket_support.answerer.drain()
```

- [ ] **Step 6: Проверить, что приложение собирается и тесты целы**

Run: `.venv/bin/python -c "import app.main" && .venv/bin/python -m pytest tests/ -q`
Expected: импорт без ошибок, вся сюита PASS, покрытие ≥ 85%

- [ ] **Step 7: Коммит**

```bash
git add app/bedolaga/__init__.py app/main.py tests/test_bedolaga_support.py && git commit -m "feat(bedolaga): wire the ticket integration into the application"
```

---

### Task 10: Скриншоты в тикетах

**Files:**
- Modify: `app/bedolaga/client.py` (метод `download_media`)
- Modify: `app/bedolaga/pipeline.py` (ветка с картинкой в `_answer`)
- Test: `tests/test_bedolaga_client.py`, `tests/test_bedolaga_pipeline.py`

**Interfaces:**
- Consumes: `app.llm.base.LlmClient.chat_with_image(user_message: str, telegram_user_id: int, base64_image: str, mime_type: str | None) -> LlmReply`, `LlmClient.supports_images() -> bool`, `app.bedolaga.types.ImageAttachment`.
- Produces: `BedolagaClient.download_media(ticket_id: int, message_id: int) -> ImageAttachment | None`.

Пользователь прикладывает к тикету скриншот куда чаще, чем описывает ошибку словами, а модель видит картинки. `media_file_id` — это Telegram file id **их** бота, скачать его нашим токеном нельзя; поэтому файл берётся через их же API: `GET /tickets/{id}/messages/{mid}/media` → `media_url` → `GET` с `X-API-Key`.

- [ ] **Step 1: Написать падающий тест клиента**

Дописать в `tests/test_bedolaga_client.py`:

```python
class TestDownloadMedia:
    """A ticket screenshot lives behind the panel's own API key."""

    async def test_returns_the_encoded_image(self) -> None:
        media_response = _response(
            200, {"media_type": "photo", "media_url": "http://bedolaga:8080/media/abc"}
        )
        file_response = MagicMock(spec=httpx.Response)
        file_response.status_code = 200
        file_response.content = b"binary-bytes"
        file_response.headers = {"content-type": "image/png"}
        client, _ = _client(get=AsyncMock(side_effect=[media_response, file_response]))

        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/png"
        assert attachment.base64_image == "YmluYXJ5LWJ5dGVz"

    async def test_defaults_the_mime_type_when_the_server_omits_it(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        file_response = MagicMock(spec=httpx.Response)
        file_response.status_code = 200
        file_response.content = b"x"
        file_response.headers = {}
        client, _ = _client(get=AsyncMock(side_effect=[media_response, file_response]))
        attachment = await client.download_media(17, 100)
        assert attachment is not None
        assert attachment.mime_type == "image/jpeg"

    async def test_returns_none_without_a_media_url(self) -> None:
        client, _ = _client(get=AsyncMock(return_value=_response(200, {"media_url": None})))
        assert await client.download_media(17, 100) is None

    async def test_returns_none_when_the_download_fails(self) -> None:
        media_response = _response(200, {"media_url": "http://bedolaga:8080/media/abc"})
        client, _ = _client(get=AsyncMock(side_effect=[media_response, httpx.ConnectError("no")]))
        assert await client.download_media(17, 100) is None
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_client.py::TestDownloadMedia -v --no-cov`
Expected: FAIL — `AttributeError: 'BedolagaClient' object has no attribute 'download_media'`

- [ ] **Step 3: Реализовать загрузку**

В `app/bedolaga/client.py` добавить импорт `import base64`, импорт `ImageAttachment` из `app.bedolaga.types`, константу под остальными:

```python
#: What the vision APIs assume when the panel does not say.
DEFAULT_MEDIA_MIME_TYPE: str = "image/jpeg"
```

и метод в класс:

```python
    async def download_media(self, ticket_id: int, message_id: int) -> ImageAttachment | None:
        """Fetch a ticket screenshot, base64-encoded for the vision APIs.

        The `media_file_id` on the message is a Telegram file id belonging to
        the Bedolaga bot, which this bot's token cannot resolve — the bytes
        have to come back through their API, under the same service key.
        """
        try:
            described = await self.http_client.get(
                f"{self.base_url}/tickets/{ticket_id}/messages/{message_id}/media",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not describe media of message %d: %s", message_id, e)
            return None

        if described.status_code != 200:
            return None

        media_url = (described.json() or {}).get("media_url")
        if not media_url:
            logger.info("Bedolaga: message %d has media the panel cannot serve", message_id)
            return None

        try:
            downloaded = await self.http_client.get(media_url, headers=self.headers)
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not download media of message %d: %s", message_id, e)
            return None

        if downloaded.status_code != 200 or not downloaded.content:
            return None

        mime_type = downloaded.headers.get("content-type") or DEFAULT_MEDIA_MIME_TYPE
        return ImageAttachment(
            base64_image=base64.b64encode(downloaded.content).decode("ascii"),
            mime_type=mime_type.split(";")[0].strip(),
        )
```

- [ ] **Step 4: Убедиться, что тесты клиента проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_client.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Написать падающий тест пайплайна**

Дописать в `tests/test_bedolaga_pipeline.py`:

```python
class TestScreenshots:
    """A ticket is often a screenshot with two words under it."""

    def _photo_ticket(self) -> Ticket:
        return _ticket(
            TicketMessage(
                id=100,
                text="",
                is_from_admin=False,
                has_media=True,
                media_type="photo",
            )
        )

    async def test_sends_the_screenshot_to_the_model(self) -> None:
        from app.bedolaga.types import ImageAttachment

        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(
            return_value=ImageAttachment(base64_image="Zm9v", mime_type="image/png")
        )
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock(return_value=LlmReply(text="Видно ошибку"))
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        args = parts["llm_client"].chat_with_image.await_args.args
        assert args[1] == TELEGRAM_ID
        assert args[2] == "Zm9v"
        assert args[3] == "image/png"
        parts["client"].reply.assert_awaited_once_with(TICKET_ID, "Видно ошибку")

    async def test_asks_about_the_picture_when_there_is_no_text(self) -> None:
        from app.bedolaga.types import ImageAttachment

        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(
            return_value=ImageAttachment(base64_image="Zm9v", mime_type="image/png")
        )
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock(return_value=LlmReply(text="Видно ошибку"))
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        assert parts["llm_client"].chat_with_image.await_args.args[0].strip() != ""

    async def test_falls_back_to_text_when_the_download_fails(self) -> None:
        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock(return_value=None)
        parts["llm_client"].supports_images = MagicMock(return_value=True)
        parts["llm_client"].chat_with_image = AsyncMock()
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["llm_client"].chat_with_image.assert_not_awaited()
        parts["llm_client"].chat.assert_awaited_once()

    async def test_ignores_media_a_text_only_model_cannot_read(self) -> None:
        answerer, parts = _answerer(ticket=self._photo_ticket())
        parts["client"].download_media = AsyncMock()
        parts["llm_client"].supports_images = MagicMock(return_value=False)
        answerer.client = parts["client"]
        answerer.llm_client = parts["llm_client"]

        await answerer.handle(TICKET_ID)

        parts["client"].download_media.assert_not_awaited()
        parts["llm_client"].chat.assert_awaited_once()
```

Тест `test_asks_about_the_picture_when_there_is_no_text` требует, чтобы `Ticket.question` для сообщения без текста возвращал заголовок тикета (это уже так по Task 2), — а если пуст и он, пайплайн подставит `bot.photo.default.prompt`.

- [ ] **Step 6: Убедиться, что тесты падают**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py::TestScreenshots -v --no-cov`
Expected: FAIL — `chat_with_image` не вызывается

- [ ] **Step 7: Добавить ветку с картинкой в пайплайн**

В `app/bedolaga/pipeline.py`, в `_answer`, заменить строку

```python
        reply = await self.llm_client.chat(question, user_key)
```

на

```python
        reply = await self.ask_model(ticket, question, user_key)
```

и добавить метод в класс:

```python
    async def ask_model(self, ticket: Ticket, question: str, user_key: int) -> LlmReply:
        """Ask the model about this ticket, with the screenshot when there is one."""
        last = ticket.last_message
        wants_vision = (
            last is not None
            and last.has_media
            and (last.media_type or "") == "photo"
            and self.llm_client.supports_images()
        )
        if not wants_vision or last is None:
            return await self.llm_client.chat(question, user_key)

        attachment = await self.client.download_media(ticket.id, last.id)
        if attachment is None:
            # Better a text-only answer than none: the panel may simply have
            # lost the file.
            return await self.llm_client.chat(question, user_key)

        prompt = question.strip() or get_message("bot.photo.default.prompt")
        return await self.llm_client.chat_with_image(
            prompt,
            user_key,
            attachment.base64_image,
            attachment.mime_type,
        )
```

Добавить `LlmReply` к импортам из `app.llm.base`.

Учёт пробелов уже защищён проверкой `if question.strip():` — тикет с одной картинкой в `/gaps` не попадёт, как и скриншот без подписи в Telegram.

- [ ] **Step 8: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/test_bedolaga_pipeline.py tests/test_bedolaga_client.py -v --no-cov`
Expected: PASS

- [ ] **Step 9: Коммит**

```bash
git add app/bedolaga/client.py app/bedolaga/pipeline.py tests/test_bedolaga_client.py tests/test_bedolaga_pipeline.py && git commit -m "feat(bedolaga): answer tickets that carry a screenshot"
```

---

### Task 11: Документация и раскатка

**Files:**
- Modify: `README.md`
- Modify: `docker-compose.yml`
- Test: `.venv/bin/python -m pytest tests/ -q` (регрессия целиком) + ручная проверка ниже

**Interfaces:**
- Consumes: настройки из Task 1, путь вебхука из Task 9.
- Produces: раздел README «Тикеты Bedolaga» и общая docker-сеть.

- [ ] **Step 1: Дать контейнерам общую сеть**

В `docker-compose.yml` добавить сервису `support-bot` вторую сеть и объявить её внешней:

```yaml
    networks:
      - internal
      - bedolaga
```

```yaml
networks:
  internal:
    driver: bridge
  # Сеть стека Bedolaga: бот ходит в её Web API, она — в вебхук бота.
  # Создаётся один раз: docker network create bedolaga-net
  bedolaga:
    external: true
    name: bedolaga-net
```

К стеку Bedolaga эта же сеть подключается на его стороне. После этого `BEDOLAGA_API_URL` — это `http://<имя контейнера bedolaga>:8080`, а бот доступен ей как `http://vpn-support-bot:8080`.

- [ ] **Step 2: Описать в README**

Добавить в `README.md` после раздела «Команды оператора» раздел:

````markdown
## Тикеты Bedolaga

Бот умеет отвечать на тикеты, которые пользователи открывают в кабинете и боте
[Bedolaga](https://github.com/fr1ngg/remnawave-bedolaga-telegram-bot), — тем же
FAQ, той же историей диалога и теми же данными Remnawave, что и в Telegram.
Ответ приходит пользователю от лица поддержки: Bedolaga сохраняет его как
сообщение админа, переводит тикет в статус `answered`, шлёт уведомление в
Telegram и обновляет кабинет. Каждый обработанный тикет зеркалится в топик
пользователя в форум-группе поддержки.

Включается переменными `BEDOLAGA_*` (см. `.env.example`). Пока
`BEDOLAGA_ENABLED=false`, ничего из этого не работает и не запускается.

### Как это устроено

- **События.** Bedolaga шлёт вебхуки `ticket.created` и `ticket.message_added`
  на `POST <бот>:8080/bedolaga/webhook` (путь настраивается). Подпись
  `X-Webhook-Signature` проверяется HMAC-SHA256 по `BEDOLAGA_WEBHOOK_SECRET`;
  без секрета эндпоинт принимает всё подряд — задавайте секрет.
- **Сверка.** Раз в `BEDOLAGA_POLL_INTERVAL_SECONDS` секунд бот сам смотрит
  тикеты в статусах `open` и `pending`. Это не роскошь: доставка вебхуков в
  Bedolaga не ретраится, упавший запрос теряется навсегда.
- **Идемпотентность.** Таблица `bedolaga_ticket_state` хранит id последнего
  отвеченного сообщения каждого тикета, поэтому повторная доставка, гонка
  вебхука с опросом и рестарт бота не приводят к второму ответу.
- **Без петель.** Собственный ответ бота возвращается событием с
  `is_from_admin=true` и отбрасывается.
- **Эскалация.** Если модель просит человека или пользователь сам его зовёт,
  к ответу добавляется строка про оператора, приоритет тикета поднимается до
  `high`, а в группу поддержки уходит алерт.
- **Оператор главнее.** Если с пользователем прямо сейчас работает живой
  оператор, бот в тикет не пишет — вопрос уходит в топик с пометкой.
- **Кабинетные аккаунты без Telegram ID** (регистрация по email или OAuth)
  получают ответ по FAQ: история такого диалога ведётся под синтетическим
  ключом, а данные подписки не запрашиваются — подтвердить, кто это, нечем.
- **Скриншоты** из тикета скачиваются через API Bedolaga и уходят в модель,
  если выбранный провайдер умеет зрение.

### Подключение

1. Создайте в Bedolaga токен Web API (админка → API-токены) и заполните
   `BEDOLAGA_API_URL`, `BEDOLAGA_API_KEY`, `BEDOLAGA_WEBHOOK_SECRET`,
   `BEDOLAGA_ENABLED=true`.
2. Свяжите контейнеры общей docker-сетью:

```bash
docker network create bedolaga-net
```

   и подключите к ней оба стека (у бота это уже описано в `docker-compose.yml`).

3. Зарегистрируйте два вебхука — по одному на событие, `event_type` в Bedolaga
   один на вебхук:

```bash
curl -X POST "$BEDOLAGA_API_URL/webhooks" \
  -H "X-API-Key: $BEDOLAGA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"support-bot: new ticket","url":"http://vpn-support-bot:8080/bedolaga/webhook","event_type":"ticket.created","secret":"'"$BEDOLAGA_WEBHOOK_SECRET"'"}'
```

```bash
curl -X POST "$BEDOLAGA_API_URL/webhooks" \
  -H "X-API-Key: $BEDOLAGA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"support-bot: ticket message","url":"http://vpn-support-bot:8080/bedolaga/webhook","event_type":"ticket.message_added","secret":"'"$BEDOLAGA_WEBHOOK_SECRET"'"}'
```

4. Перезапустите бота и откройте тестовый тикет. Доставки видно в
   `GET /webhooks/stats` на стороне Bedolaga, обработку — в логах бота
   (`Bedolaga ticket ...`).
````

Также дописать строки в таблицу переменных окружения README: `BEDOLAGA_ENABLED`,
`BEDOLAGA_API_URL`, `BEDOLAGA_API_KEY`, `BEDOLAGA_WEBHOOK_SECRET`,
`BEDOLAGA_WEBHOOK_PATH`, `BEDOLAGA_POLL_INTERVAL_SECONDS`.

- [ ] **Step 3: Прогнать всё**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy`
Expected: тесты PASS с покрытием ≥ 85%, ruff и mypy без замечаний

- [ ] **Step 4: Коммит**

```bash
git add README.md docker-compose.yml && git commit -m "docs(bedolaga): document and wire the ticket integration"
```

---

## Ручная проверка на живом стенде

Автотесты не проверяют главного: что чужая панель отвечает так, как описано выше. Перед мержем:

1. `BEDOLAGA_ENABLED=true`, перезапуск бота. В логах — `Bedolaga ticket integration enabled: ...` и `Bedolaga webhook endpoint registered at ...`.
2. Проверить доступность изнутри сети Bedolaga:

```bash
docker compose exec bedolaga curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://vpn-support-bot:8080/bedolaga/webhook -H 'X-Webhook-Event: ticket.created' -d '{}'
```

   Ожидание: `403` при заданном секрете (подписи нет) — эндпоинт живой и защищён.
3. Открыть тикет от тестового аккаунта в кабинете. Ожидание: ответ в тикете за секунды, уведомление в Telegram у пользователя, зеркало в топике группы поддержки.
4. Ответить в тот же тикет второй раз. Ожидание: второй ответ; в `bedolaga_ticket_state` для тикета `last_answered_message_id` вырос.
5. Попросить в тикете оператора («хочу человека»). Ожидание: приписка про оператора, `priority=high` в панели, алерт в группе.
6. Выключить бота на минуту, написать в тикет, включить обратно. Ожидание: сверочный проход подберёт сообщение в течение `BEDOLAGA_POLL_INTERVAL_SECONDS`.
7. Проверить отсутствие петли: в тикете ровно один ответ бота на одно сообщение пользователя.

## Что осталось за рамками

- **Реакция на `ticket.status_changed`** — закрытие тикета оператором ничего не меняет для бота: закрытый тикет он и так не трогает.
- **Ответ картинкой из FAQ.** В Telegram бот досылает иллюстрацию из `faq/images`; в тикет она не уходит — API Bedolaga принимает только `media_file_id` их собственного бота, а загрузки файла у него нет. Иллюстрация упоминается только текстом ответа.
- **Автозакрытие тикетов** после ответа бота — статус `answered` панель ставит сама, дальше решает человек.
