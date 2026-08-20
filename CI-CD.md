# CI/CD Pipeline — SupportAiBot

Бот и MCP-сервер собираются и публикуются независимо, как два отдельных образа.
Связи между их пайплайнами нет: `docker-compose.yml` сводит их вместе уже на
сервере.

```
push → SupportAiBot (master)
  └─ test (pytest & ruff) → build → mitetenov/supportbot:latest + :{version} + :{sha}
                                       └─ Deploy (вручную, workflow_dispatch) → SSH на сервер

push → mcp-remnawave (main)
  └─ build → GitHub Release (mcp-release.zip) + mitetenov/remnawave-mcp:latest + :v{version} + :{sha}
```

## SupportAiBot

`.github/workflows/docker-multiarch.yml`, триггеры: push в `master`, pull request
в `master`, `workflow_dispatch`.

| Job | Когда | Что делает |
|-----|-------|------------|
| `test` | всегда, включая PR | `ruff check .`, `ruff format --check .`, `mypy` и `pytest -v` (с порогом покрытия 85%) на Python 3.14 (`uv`). Это merge-gate: именно его стоит требовать в branch protection |
| `build` | только не-PR (`master`, ручной запуск) | multi-arch образ (linux/amd64 + linux/arm64), пуш в Docker Hub |

Теги образа: `latest`, `:{version}`, `:{sha}`.

`latest` и `:{sha}` перезаписываются свободно, а версия — нет: заменить образ,
на который указывает тег версии, значит остаться без того, на что откатываться.

Поэтому версия не редактируется под каждый мерж. Руками в `pyproject.toml`
задаётся только **мажорная** часть; минорную выбирает
`.github/scripts/next_version.py`: он берёт список тегов
`mitetenov/supportbot` с Docker Hub, находит старшую опубликованную минорную
версию в этой мажорной линии и публикует следующую. Патч всегда `0`.

Источник истины — реестр, а не репозиторий. Это то, с чем тег и столкнулся бы;
не нужен коммит обратно в master, поэтому два мержа, пришедшие одновременно, не
могут выбрать один номер; а прогон, который успел запушить образ и упал после,
просто учитывается следующим.

Сравнение числовое, не строковое: среди опубликованных есть `1.6.9` и `1.6.17`,
и как строки они сравниваются наоборот. Тег с суффиксом (`2.4.0-rc1`) счётчик не
двигает. Если реестр недоступен, шаг падает — считать «ничего не опубликовано»
значило бы перезаписать существующий образ.

Проверка «тег ещё не занят» осталась на месте после логина как вторая пара глаз:
теперь её срабатывание означает, что список тегов пришёл неполным, и повторять
прогон вслепую нельзя.

Мажорную версию поднимайте в `pyproject.toml` и следом выполняйте `uv lock` —
иначе шаг `uv lock --check` в job `test` упадёт на рассинхроне.

`.github/workflows/deploy.yml` — отдельный ручной workflow (`workflow_dispatch`,
на вход тег). Ходит по SSH на сервер, выставляет `BOT_TAG` и делает
`docker compose pull && docker compose up -d`. Автоматически после сборки не
запускается.

## mcp-remnawave

Живёт в [mitetenov/mcp-remnawave](https://github.com/mitetenov/mcp-remnawave),
собирается своим CI на Node 22.

| Workflow | Когда | Что делает |
|----------|-------|------------|
| `pr.yml` | pull request в `main` | `tsc --noEmit`, `npm test`, `npm run build` |
| `ci.yml` | push в `main` | build → GitHub Release с `mcp-release.zip` → multi-arch образ в Docker Hub |

Теги образа: `latest`, `:v{version из package.json}`, `:{sha}`. Тег релиза берётся
оттуда же, поэтому версию в `package.json` нужно поднимать в том же PR, что и
изменения — иначе релиз с этим тегом уже существует.

Бот подключается к MCP по HTTP (`REMNAWAVE_MCP_URL`), образ в compose закреплён
через `MCP_TAG` — намеренно не `latest`: набор инструментов MCP зависит от его
версии и от `REMNAWAVE_IS_SUPPORT`, так что молчаливое обновление образа может
незаметно забрать у бота инструменты.

`mcp-release.zip` остаётся артефактом релиза, но в сборке бота не участвует:
Dockerfile SupportAiBot его не скачивает, MCP запускается отдельным сервисом
`mcp-remnawave` из compose.

## Docker-образы

| | Бот | MCP |
|---|---|---|
| Репозиторий | `mitetenov/supportbot` | `mitetenov/remnawave-mcp` |
| Теги | `latest`, `:{version}`, `:{sha}` | `latest`, `:v{version}`, `:{sha}` |
| Платформы | `linux/amd64`, `linux/arm64` | `linux/amd64` |
| Кэш | GitHub Actions Cache (`type=gha, mode=max`) | нет |

> MCP-образ собирается без `platforms:`, то есть только под архитектуру раннера —
> `linux/amd64`. На arm64-хосте он пойдёт через эмуляцию (или не запустится
> вовсе), в отличие от образа бота. Если сервер на arm — в `ci.yml`
> mcp-remnawave нужно дописать `platforms: linux/amd64,linux/arm64`.

## Secrets

| Secret | Где хранится | Назначение |
|--------|-------------|------------|
| `DOCKER_USERNAME` | SupportAiBot + mcp-remnawave | Логин Docker Hub |
| `DOCKER_TOKEN` | SupportAiBot + mcp-remnawave | Токен Docker Hub |
| `SERVER_HOST` / `SERVER_USER` / `SERVER_SSH_KEY` / `SERVER_PORT` | SupportAiBot | Доступ для ручного деплоя по SSH |
