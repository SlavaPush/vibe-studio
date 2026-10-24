# vibe-studio

Монорепозиторий двух приложений: публичного AI-чата и мультиагентного офиса, который развивает этот чат.

## Состав

| Каталог | Назначение | Документация |
| --- | --- | --- |
| `apps/chat/` | `vibe-chat` — публичный AI-чат: отдельные диалоги участников, проверка каждого сообщения, маршрутизация по сложности, генерация изображений, постоянная история. FastAPI (`api/`), React + Vite (`web/`), контракт API (`contracts/`), Docker-образ и локальная PostgreSQL (`infra/`) | [apps/chat/README.md](apps/chat/README.md) |
| `apps/office/` | Мультиагентный офис: приём задач участников, роли на LangGraph, проверки и выпуск изменений чата. Каталог появится после импорта офиса | — |
| `.github/workflows/ci.yml` | Общий CI для всех приложений | — |
| `.github/CODEOWNERS` | Владелец защищённых путей | — |

Запуск, переменные окружения и деплой каждого приложения описаны в его README.

## Проверки

Команды выполняются из корня репозитория. Это те же проверки, что в CI.

```bash
docker compose -f apps/chat/infra/docker-compose.yml up -d
(cd apps/chat/api && uv sync --locked && uv run pytest)
(cd apps/chat/web && npm ci && npm run lint && npm run test && npm run build)
docker build -f apps/chat/infra/Dockerfile -t vibe-chat:local apps/chat
```

Образ чата собирается с контекстом `apps/chat`: пути в `apps/chat/infra/Dockerfile` и `apps/chat/.dockerignore` заданы относительно этого каталога.

## CI

`.github/workflows/ci.yml` запускается при `push` в `main` и для каждого pull request, без фильтров путей. Job чата:

| Job | Проверка |
| --- | --- |
| `chat-api` | `uv run pytest` на PostgreSQL 16 |
| `chat-web` | `npm ci`, lint, тесты, сборка |
| `chat-containers` | сборка образа, запуск с PostgreSQL, `/healthz`, версия в `<meta name="app-version">`, `404` для неизвестного `/api/...`, процесс не от root |

Job офиса добавляются с префиксом `office-` после импорта офиса.

## CODEOWNERS

`.github/CODEOWNERS` назначает владельца `@SlavaPush` для офиса, серверной части, контракта и инфраструктуры чата, CI, manifest и lock-файлов, клиента API и mock-данных интерфейса, конфигураций сборки и lint, а также тестов. Изменения остальных файлов интерфейса чата не требуют одобрения владельца. Правило действует, когда в настройках репозитория включено обязательное одобрение владельцев кода.

## Лицензия

MIT, см. `LICENSE`.
