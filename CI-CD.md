# CI/CD Pipeline — SupportAiBot

## Trigger chain

```
push → SupportAiBot (master)
  └─ Docker Multi-Arch Build & Push → mitetenov/supportbot:latest

push → mcp-remnawave (main)
  └─ CI builds + creates GitHub Release with mcp-release.zip
       └─ repository_dispatch (event_type: mcp-release)
            └─ SupportAiBot Docker Multi-Arch Build & Push
```

### 1. Bot push (`push: [master]`)
При пуше в `master` репозитория **mitetenov/SupportAiBot**:
- Собирается multi-arch образ (linux/amd64 + linux/arm64)
- Пушится в Docker Hub как `mitetenov/supportbot:latest` + `:sha`

### 2. MCP release (`repository_dispatch`)
При новом релизе **mitetenov/mcp-remnawave** CI посылает `repository_dispatch` с типом `mcp-release`:
- Запускается тот же Docker build workflow
- Dockerfile скачивает pre-built `mcp-release.zip` из свежего релиза MCP

### 3. Ручной запуск (`workflow_dispatch`)
Из интерфейса GitHub Actions можно запустить сборку вручную.

## Docker-образ

| Параметр | Значение |
|----------|----------|
| Репозиторий | `mitetenov/supportbot` |
| Теги | `latest`, `:{sha}` |
| Платформы | `linux/amd64`, `linux/arm64` |
| Кэш | GitHub Actions Cache (`type=gha, mode=max`) |

## Secrets

| Secret | Где хранится | Назначение |
|--------|-------------|------------|
| `DOCKER_USERNAME` | SupportAiBot → Settings → Secrets | Логин Docker Hub |
| `DOCKER_TOKEN` | SupportAiBot → Settings → Secrets | Токен Docker Hub |
| `PAT_SUPPORTBOT_DISPATCH` | mcp-remnawave → Settings → Secrets | PAT для отправки `repository_dispatch` в SupportAiBot |

## MCP сборка

MCP сервер (`mcp-remnawave`) собирается отдельно в своём CI и публикуется как release asset (`mcp-release.zip`):
- Репозиторий: [mitetenov/mcp-remnawave](https://github.com/mitenetov/mcp-remnawave)
- CI workflow: `.github/workflows/ci.yml`
- Артефакт: `mcp-release.zip` (tsup-bundled single-file dist)

Dockerfile SupportAiBot скачивает этот zip через `curl` + `unzip` — без Node.js/npm в сборочной стадии.

## Требования

- PAT в `PAT_SUPPORTBOT_DISPATCH` должен иметь `repo` доступ к **mitetenov/SupportAiBot**
- MCP CI workflow должен быть настроен на отправку `repository_dispatch` после создания релиза
