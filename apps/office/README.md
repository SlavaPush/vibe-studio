# back-office

Мультиагентный офис, который развивает публичный сервис [vibe-chat](https://github.com/SlavaPush/vibe-chat). Участники ставят задачи по разработке сервиса; фильтр приёма отклоняет посторонние задачи; до трёх задач выполняются одновременно, каждая в отдельном git worktree; пять ролей (исследователь, Supervisor, разработчик, QA, критик) на LangGraph; независимые проверки; очередь выпуска; подтверждение релиза администратором.

Для демонстрации офис работает локально. Архитектура допускает деплой на сервер с Docker без переписывания.

## Состав

| Каталог | Назначение |
| --- | --- |
| `api/` | FastAPI + LangGraph: участники, задачи, слоты, граф ролей, HITL, выпуск |
| `web/` | React + TypeScript + Vite: панель задач |
| `contracts/` | Контракт API панели и описание интеграций с GitHub и Render (этап P1) |
| `policies/` | Разрешённые пути и команды sandbox, профили моделей `models.yaml` |
| `docker/sandbox/` | Образ для сборки и проверок изменяемого кода (Node 24 + Playwright) |
| `api/scripts/` | Smoke-проверки локальных моделей |
| `workspace/` | Клон `vibe-chat` и worktree задач; в `.gitignore` |

Статус: этап `P1` — контракты `contracts/api.md` и `contracts/integrations.md`, Pydantic-схемы, тестовые данные API и панель на тестовых данных. Граф ролей, запись решений, SSE и серверная проверка прав ещё не реализованы (этап `P4`).

## Контракты

`contracts/api.md` описывает API панели: операции, модели, перечисления, ошибки и права. Примеры JSON размечены маркером `<!-- example: <Модель> -->`. `api/tests/test_contract.py` разбирает каждый пример своей Pydantic-моделью. Скрипт `web/scripts/extract-contract-examples.mjs` извлекает те же примеры в `web/src/mocks/contract-examples.json` перед `npm run dev`, `npm run test` и `npm run build`; файл создаётся автоматически и не хранится в Git.

`contracts/integrations.md` описывает работу офиса с GitHub, Render и `GET /api/version` сервиса `vibe-chat`: ветки задач, PR, обязательные проверки, очередь выпуска, деплой по `commitId` и откат.

## Структура `web/src`

| Путь | Назначение |
| --- | --- |
| `api/types.ts` | Типы и перечисления по `contracts/api.md` |
| `components/ui/` | Компоненты shadcn/ui (`components.json`: `new-york`, `neutral`) |
| `components/office/` | Экраны панели: `AccessGate`, `TaskStack`, `TaskCard`, `ReleaseQueue` |
| `lib/permissions.ts` | `canAct`: доступность решений по владельцу, роли и статусу |
| `lib/slots.ts` | `countSlots`: подсчёт занятых слотов |
| `lib/labels.ts` | Русские подписи статусов, решений, ролей и событий |
| `mocks/` | Тестовые данные: примеры контракта и данные только для панели |
| `test/` | Тесты vitest и React Testing Library |
| `index.css` | UI-токены shadcn/ui, тёмная тема `.dark`, focus и `prefers-reduced-motion` |

Панель работает только на тестовых данных и не выполняет сетевых запросов. Тестовые коды входа: `OFFICE-2026` (участник) и `ADMIN-2026` (администратор). Переключатель «Тестовый участник» показывает ограничения действий для владельца задачи, другого участника и администратора.

## Профили моделей

`policies/models.yaml` задаёт модель и уровень усилий для каждой роли в профилях `local` (Ollama), `cloud` (Anthropic с резервом OpenAI) и `hybrid` (локальная модель для коротких шагов, облачная для разработчика, критика и фильтра приёма). Раздел `capabilities` описывает параметр рассуждения каждой пары провайдер/модель; поле `verified` и список `verified_values` заполняются после реального вызова (smoke-проверки этапа `P0`). Код читает файл начиная с этапа `P4`.

Локальные ограничения: одна загруженная модель Ollama, основная модель `qwen3:8b`. Приложение Ollama на macOS читает переменные окружения через `launchctl`; после команды перезапустить Ollama:

```bash
launchctl setenv OLLAMA_MAX_LOADED_MODELS 1
```

Docker Desktop на машине владельца ограничен 8 GB памяти. Одновременно запускать не больше одного-двух контейнеров проверок при загруженной локальной модели.

## Запуск локально

Требования: Python 3.12 и [uv](https://docs.astral.sh/uv/), Node 24, Docker, [Ollama](https://ollama.com) с моделью `qwen3:8b`.

```bash
cp .env.example .env
```

```bash
ollama pull qwen3:8b
```

API:

```bash
cd api && uv sync && uv run uvicorn app.main:app --reload --port 8100
```

Панель:

```bash
cd web && npm ci && npm run dev
```

Проверки:

```bash
cd api && uv run pytest
```

```bash
cd web && npm ci && npm run lint && npm run test && npm run build
```

Smoke-проверка локальной модели (ответ без рассуждения и с рассуждением, код возврата 0 только при двух завершённых ответах):

```bash
cd api && uv run python scripts/smoke_ollama.py qwen3:8b
```

Образ sandbox и сквозная проверка: сборка `vibe-chat/web` внутри контейнера и открытие собранного приложения в браузере Playwright. Каталог `web` репозитория `vibe-chat` монтируется только для чтения:

```bash
docker build -t back-office-sandbox docker/sandbox
```

```bash
docker run --rm -v "$PWD/../vibe-chat/web:/src:ro" back-office-sandbox sandbox-smoke /src
```

## Лицензия

MIT, см. `LICENSE`.
