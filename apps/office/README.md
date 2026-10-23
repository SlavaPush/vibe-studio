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

Статус: подэтап `P4.1` — API офиса на БД (SQLite локально, PostgreSQL при деплое): сеансы по коду доступа, проверка `Host` и `Origin`, права, фильтр приёма задач (детерминированные проверки, модерация OpenAI, классификатор), три слота и очередь `queued`, решения с версией и идемпотентностью, журнал событий и SSE, отчёты проверок и артефакты, решения по выпускам без деплоя. Подэтап `P4.2` — рабочие копии и sandbox: клон `vibe-chat`, worktree на задачу, политика путей и команд, проверки в Docker, commit кодом офиса, `GET /api/tasks/{id}/diff`. Подэтап `P4.3` — граф ролей LangGraph: шлюз моделей, требования Supervisor и подтверждение владельца, разработчик, проверки, QA и критик, циклы исправления, бюджеты, восстановление после перезапуска. Переход панели на API — подэтап `P4.4`; панель пока работает на тестовых данных.

## Граф ролей

Задача, занявшая слот, выполняется графом LangGraph (`api/app/graph.py`) в отдельном потоке; одновременно работает не больше трёх графов (`api/app/runner.py`). Порядок узлов:

1. Supervisor готовит требования: краткое описание и до восьми критериев приёмки. При необходимости исследователь загружает до трёх публичных страниц `https` (`api/app/research.py`): адрес сервера проверяется до соединения, соединение идёт на проверенный IP, прокси окружения не используются.
2. Требования записываются в задачу с `spec_hash`, событие `decision_required` ждёт решения `approve_spec`. Граф продолжает только при записи решения в таблице `decisions` для версии из этого события.
3. Разработчик выполняет до шести ходов: чтение, запись и удаление файлов через политику путей, запуск `lint`, `test`, `build`. Commit делает код офиса с trailer `Office-Role` и `Office-Round`. Затем запускается полный набор проверок; при неуспехе задача возвращается разработчику не больше двух раз.
4. QA и критик работают параллельно. QA пишет тест Playwright `web/e2e/office/<task_id>.pw.ts`; тест должен не проходить на `base_sha` и проходить на версии задачи. Результат определяется по JSON-отчёту Playwright. Критик проверяет diff по требованиям.
5. Блокирующее замечание возвращает задачу разработчику (не больше двух циклов, `fix_cycles`), затем `needs_attention`. Без блокирующих замечаний задача переходит в `awaiting_integration`; push и PR — этап `P5`.

Ответ модели — JSON по схеме роли; ответ вне схемы или обрезанный пределом выходных токенов не исполняется, задача получает `failed` с отдельной причиной для каждого случая. В схемах ходов длинные текстовые поля идут последними: `actions` — последнее поле ответа, `content` — последнее поле действия. При другом порядке `qwen3:8b` в части ответов продолжала поле `content` строками комментариев до предела токенов. Каждое обращение к модели, включая резервного провайдера, учитывается в `usage` до вызова; после 30 обращений задача получает `needs_attention`. Предел времени задачи — `OFFICE_TASK_TIME_LIMIT_MINUTES` (по умолчанию 60) от перехода в `developing`. Обращения к Ollama выполняются по одному.

Checkpoint графа хранится в `<имя БД>.checkpoints.sqlite` рядом с SQLite-файлом офиса или в той же БД PostgreSQL. После перезапуска API продолжает графы задач в статусах `awaiting_spec_approval`, `developing`, `reviewing`. Выход задачи из статусов слота (`cancel`, `needs_attention`, `failed`) останавливает граф и удаляет контейнеры задачи; worktree отменённой задачи удаляется после остановки графа. Контейнеры проверок помечаются меткой `back-office.workspace=<хеш каталога рабочей области>`; при старте API удаляет контейнеры с этой меткой, оставшиеся от прежнего процесса. `retry` начинает новую попытку с сохранёнными требованиями, worktree, `usage` и `fix_cycles`; требования подтверждаются заново.

## Рабочая область и sandbox

Офис держит клон `TARGET_REPO_URL` в `<WORKSPACE_DIR>/vibe-chat` (по умолчанию `workspace/` в корне репозитория). Задача, занявшая слот, получает worktree `<WORKSPACE_DIR>/wt/<task_id>` на ветке `office/<task_id>` от `origin/main`; `branch` и `base_sha` записываются в задачу. Отмена и выпуск удаляют worktree, ветка остаётся. При `failed` и `needs_attention` worktree сохраняется, `retry` использует его повторно. При старте API создаёт недостающие worktree задач в статусах слота и удаляет worktree отменённых и выпущенных задач. Без `TARGET_REPO_URL` API не запускается.

`policies/paths.yaml` задаёт пути, которые роли читают и пишут: разработчик — `web/src/**`, `web/public/**`, `web/index.html`; QA — только `web/e2e/office/**/*.pw.ts`. Запреты общего списка действуют без учёта регистра. Symlink в любом компоненте пути, `..`, абсолютные пути, недопустимые расширения и файлы больше 200 КБ отклоняются. Каждый вызов инструмента пишет событие `tool_called`. Commit выполняет код офиса: каждый изменённый путь проверяется повторно, исполняемые файлы, symlink и submodule отклоняются.

`policies/commands.yaml` задаёт шаги проверок `install`, `lint`, `test`, `build`, `e2e`, их таймауты, роли и пределы контейнера. Контейнер запускается от пользователя `10001` с `--cap-drop ALL`, `no-new-privileges`, корневой файловой системой только для чтения, пределами памяти, CPU и процессов и без сети (кроме `install`). Worktree монтируется только для чтения. Зависимости ставятся `npm ci` в том Docker `office-nm-<хеш lock-файла>`, общий для задач с одинаковым `web/package-lock.json`. Шаг `e2e` собирает приложение (ошибка сборки — код выхода 90) и запускает тесты QA `web/e2e/office/**/*.pw.ts` в Playwright; JSON-отчёт `playwright-report.json` сохраняется в артефактах. Одновременно работает не больше `OFFICE_SANDBOX_CONTAINERS` контейнеров. Журналы шагов, скриншоты и трассы доступны через `GET /api/tasks/{id}/artifacts/{name}`.

Образ sandbox собирается вручную (без образа шаги завершаются ошибкой «Sandbox недоступен»):

```bash
docker build -t back-office-sandbox:p4 docker/sandbox
```

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

API офиса на `127.0.0.1:8001` (переменные читаются из `../.env`; без `OFFICE_ACCESS_CODE` и `OFFICE_ADMIN_CODE` вход запрещён):

```bash
cd api && uv sync && uv run --env-file ../.env uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
```

Состояние офиса хранится в `OFFICE_DATABASE_URL` (по умолчанию `api/office.sqlite`). Схема `api/app/schema.sql` применяется при старте. Фильтр приёма читает `policies/intake.yaml` (запрещённые пути и команды) и профиль `intake_filter` из `policies/models.yaml`.

Панель:

```bash
cd web && npm ci && npm run dev
```

Проверки:

```bash
cd api && uv run pytest
```

Тесты БД выполняются на SQLite и, при заданной `OFFICE_TEST_POSTGRES_URL`, на PostgreSQL. Каждый тест получает отдельную схему PostgreSQL. Локальная PostgreSQL в Docker:

```bash
docker run -d --rm --name office-pg-test -e POSTGRES_USER=office -e POSTGRES_PASSWORD=office -e POSTGRES_DB=office -p 127.0.0.1:55432:5432 postgres:17
```

```bash
cd api && OFFICE_TEST_POSTGRES_URL=postgresql://office:office@127.0.0.1:55432/office uv run pytest
```

Тесты рабочей области используют локальный bare-репозиторий и не обращаются к сети. Тесты с реальным Docker помечены `docker` и запускаются отдельно. Они требуют собранный образ `back-office-sandbox:p4` и клонируют `vibe-chat` из `OFFICE_TEST_TARGET_REPO` (по умолчанию `https://github.com/SlavaPush/vibe-chat.git`; подходит и путь к локальной копии):

```bash
cd api && OFFICE_TEST_DOCKER=1 uv run pytest -m docker
```

```bash
cd web && npm ci && npm run lint && npm run test && npm run build
```

Smoke-проверка локальной модели (ответ без рассуждения и с рассуждением, код возврата 0 только при двух завершённых ответах):

```bash
cd api && uv run python scripts/smoke_ollama.py qwen3:8b
```

Smoke-проверка шлюза ролей: по одному реальному вызову Anthropic, OpenAI и Ollama со схемой ответа QA (вызовы облачных провайдеров платные; код возврата 0 только при ответах по схеме от всех проверенных провайдеров). Аргументы ограничивают проверку названными провайдерами, например только локальной моделью:

```bash
cd api && PYTHONPATH=. uv run --env-file ../.env python scripts/smoke_roles.py
cd api && PYTHONPATH=. uv run --env-file ../.env python scripts/smoke_roles.py ollama
```

Образ sandbox и сквозная проверка: сборка `vibe-chat/web` внутри контейнера и открытие собранного приложения в браузере Playwright. Каталог `web` репозитория `vibe-chat` монтируется только для чтения:

```bash
docker build -t back-office-sandbox:p4 docker/sandbox
```

```bash
docker run --rm -v "$PWD/../vibe-chat/web:/src:ro" back-office-sandbox:p4 sandbox-smoke /src
```

## Лицензия

MIT, см. `LICENSE`.
