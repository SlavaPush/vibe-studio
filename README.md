# vibe-studio

Монорепозиторий двух приложений: публичного AI-чата и мультиагентного офиса, который развивает этот чат.

## Состав

| Каталог | Назначение | Документация |
| --- | --- | --- |
| `apps/chat/` | `vibe-chat` — публичный AI-чат: отдельные диалоги участников, проверка каждого сообщения, маршрутизация по сложности, генерация изображений, постоянная история. FastAPI (`api/`), React + Vite (`web/`), контракт API (`contracts/`), Docker-образ и локальная PostgreSQL (`infra/`) | [apps/chat/README.md](apps/chat/README.md) |
| `apps/office/` | Мультиагентный офис: приём задач участников, роли на LangGraph, проверки и выпуск изменений чата. FastAPI + LangGraph (`api/`), панель React + Vite (`web/`), контракты (`contracts/`), политики путей, команд и моделей (`policies/`), образ sandbox (`docker/sandbox/`). Офис ставит задачи в этот же монорепозиторий: клон в `apps/office/workspace/vibe-studio`, worktree задачи со sparse checkout только `apps/chat/`, изменения только в `apps/chat/web/` | [apps/office/README.md](apps/office/README.md) |
| `.github/workflows/ci.yml` | Общий CI для всех приложений | — |
| `.github/workflows/office-paths.yml` | Проверка путей веток задач офиса `office/*` | — |
| `.github/CODEOWNERS` | Владелец защищённых путей | — |

Запуск, переменные окружения и деплой каждого приложения описаны в его README.

## Проверки

Команды выполняются из корня репозитория. Это те же проверки, что в CI.

```bash
docker compose -f apps/chat/infra/docker-compose.yml up -d
(cd apps/chat/api && uv sync --locked && uv run pytest)
(cd apps/chat/web && npm ci && npm run lint && npm run test && npm run build)
docker build -f apps/chat/infra/Dockerfile -t vibe-chat:local apps/chat
(cd apps/office/api && uv sync --locked && uv run pytest)
(cd apps/office/web && npm ci && npm run lint && npm run test && npm run build)
docker build -t back-office-sandbox:p4 apps/office/docker/sandbox
docker run --rm -v "$PWD/apps/chat/web:/src:ro" -v "$PWD/apps/chat/contracts:/work/contracts:ro" back-office-sandbox:p4 sandbox-smoke /src
(cd apps/office/api && OFFICE_TEST_DOCKER=1 OFFICE_TEST_TARGET_REPO="$PWD/../../.." uv run pytest -m docker)
uvx --from actionlint-py actionlint .github/workflows/*.yml
```

Docker-тесты офиса клонируют `OFFICE_TEST_TARGET_REPO` и берут его ветку `main`: локально проверяется `apps/chat` закоммиченной ветки `main`. PostgreSQL-тесты офиса запускаются при заданной `OFFICE_TEST_POSTGRES_URL` (см. `apps/office/README.md`).

Образ чата собирается с контекстом `apps/chat`: пути в `apps/chat/infra/Dockerfile` и `apps/chat/.dockerignore` заданы относительно этого каталога.

## CI

`.github/workflows/ci.yml` запускается при `push` в `main` и для каждого pull request, без фильтров путей. Job:

| Job | Проверка |
| --- | --- |
| `chat-api` | `uv run pytest` на PostgreSQL 16 |
| `chat-web` | `npm ci`, lint, тесты, сборка |
| `chat-containers` | сборка образа, запуск с PostgreSQL, `/healthz`, версия в `<meta name="app-version">`, `404` для неизвестного `/api/...`, процесс не от root |
| `office-api` | `uv run pytest` офиса на SQLite и PostgreSQL 17 |
| `office-web` | `npm ci`, lint, тесты, сборка панели офиса |
| `office-sandbox` | сборка образа sandbox, `sandbox-smoke` на `apps/chat/web` того же checkout, `pytest -m docker` с `OFFICE_TEST_TARGET_REPO`, указывающим на этот checkout; ветка `main` создаётся на проверяемом commit |

`.github/workflows/office-paths.yml` — проверка путей веток задач офиса. Событие `pull_request_target`, job `office-paths` выполняется только для веток `office/*`. Checkout базовой ветки без `ref` и с `persist-credentials: false`, объекты head PR получаются `git fetch` только как данные, код из head не выполняется. Скрипт офиса `apps/office/api/scripts/check_branch.py` из базовой ревизии проверяет каждый изменённый путь по политике `apps/office/policies/paths.yaml`, режимы файлов, двоичное содержимое и комментарии отключения lint и TypeScript. Права `contents: read`, секретов нет. Пути вне зоны интерфейса `apps/chat/web/` отклоняются.

## CODEOWNERS

`.github/CODEOWNERS` назначает владельца `@SlavaPush` для офиса, серверной части, контракта и инфраструктуры чата, CI, manifest и lock-файлов, клиента API и mock-данных интерфейса, конфигураций сборки и lint, а также тестов. Изменения остальных файлов интерфейса чата не требуют одобрения владельца. Правило действует, когда в настройках репозитория включено обязательное одобрение владельцев кода: его включает ruleset `main`.

## Ruleset `main`

`.github/rulesets/main.json` — определение ruleset ветки по умолчанию (`~DEFAULT_BRANCH`) в формате импорта GitHub, `enforcement: active`.

Ruleset требует:

- запрет удаления ветки (`deletion`) и force push (`non_fast_forward`);
- изменения только через pull request: 0 обязательных одобрений, одобрение владельца кода по `.github/CODEOWNERS` для файлов с владельцем, снятие прежних одобрений после нового push;
- успешные обязательные проверки `chat-api`, `chat-web`, `chat-containers`, `office-api`, `office-web`, `office-sandbox` и `office-paths` от GitHub Actions (`integration_id: 15368`), без требования актуальной базы. Статус с тем же именем от другого приложения или пользователя не засчитывается. Для веток не `office/*` job `office-paths` пропускается, GitHub считает пропуск успехом.

Обходит ruleset только роль администратора репозитория (`RepositoryRole`, `actor_id: 5`, `bypass_mode: always`): владелец может выполнять push в `main` напрямую. GitHub App офиса в списке обхода нет: офис не может отправить commit в `main` и слить PR в обход правил.

Тест офиса `apps/office/api/tests/test_ruleset.py` сверяет `main.json` с workflow: имена обязательных проверок равны id job в `.github/workflows/*.yml`. При добавлении или переименовании job обновите `main.json` и ruleset на GitHub.

Как применить:

1. Settings → Rules → Rulesets → New ruleset → Import a ruleset.
2. Выбрать файл `.github/rulesets/main.json` и сохранить.

Если импорт не принимает файл, задайте те же значения вручную.

Как сверить:

1. `GET /repos/SlavaPush/vibe-studio/rulesets` возвращает `id` ruleset `main`.
2. `GET /repos/SlavaPush/vibe-studio/rulesets/{id}` возвращает действующий ruleset.
3. Сравнить с `main.json` поля `name`, `target`, `enforcement`, `conditions`, `bypass_actors` и параметры всех правил. Служебные поля ответа (`id`, `node_id`, `_links`, `source`, `source_type`, `created_at`, `updated_at`, `current_user_can_bypass`) не сравниваются.

## Лицензия

MIT, см. `LICENSE`.
