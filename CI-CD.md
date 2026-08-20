# CI/CD Pipeline — SupportAiBot

Бот и MCP-сервер собираются и публикуются независимо, как два отдельных образа.
Связи между их пайплайнами нет: `docker-compose.yml` сводит их вместе уже на
сервере.

```
push → SupportAiBot (master)
  └─ test (mvn test) → build → mitetenov/supportbot:latest + :{version} + :{sha}
                                  └─ Deploy (вручную, workflow_dispatch) → SSH на сервер

push → mcp-remnawave (main)
  └─ build → GitHub Release (mcp-release.zip) + mitetenov/remnawave-mcp:latest + :v{version} + :{sha}
```

## SupportAiBot

`.github/workflows/docker-multiarch.yml`, триггеры: push в `master`, pull request
в `master`, `workflow_dispatch`.

| Job | Когда | Что делает |
|-----|-------|------------|
| `test` | всегда, включая PR | `mvn -B test -pl bot` на JDK 21. Это merge-gate: именно его стоит требовать в branch protection |
| `build` | только не-PR (`master`, ручной запуск) | multi-arch образ (linux/amd64 + linux/arm64), пуш в Docker Hub |

Теги образа: `latest`, `:{version из pom.xml}`, `:{sha}`.

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
