# vibe-studio

Монорепозиторий двух приложений: публичного AI-чата и мультиагентного офиса, который развивает этот чат.

Публичный чат: https://elbrus-vibe-chat.onrender.com. Вход — по личному коду участника. Демонстрационные запросы для защиты — [docs/demo.md](docs/demo.md).

## Состав

| Каталог | Назначение | Документация |
| --- | --- | --- |
| `apps/chat/` | `vibe-chat` — публичный AI-чат: отдельные диалоги участников, проверка каждого сообщения, маршрутизация по сложности, генерация изображений, постоянная история. FastAPI (`api/`), React + Vite (`web/`), контракт API (`contracts/`), Docker-образ и локальная PostgreSQL (`infra/`) | [apps/chat/README.md](apps/chat/README.md) |
| `apps/office/` | Мультиагентный офис: приём задач участников, роли на LangGraph, проверки и выпуск изменений чата. FastAPI + LangGraph (`api/`), панель React + Vite (`web/`), контракты (`contracts/`), политики путей, команд и моделей (`policies/`), образ sandbox (`docker/sandbox/`). Офис ставит задачи в этот же монорепозиторий: клон в `apps/office/workspace/vibe-studio`, worktree задачи со sparse checkout только `apps/chat/`, изменения только в `apps/chat/web/` | [apps/office/README.md](apps/office/README.md) |
| `.github/workflows/ci.yml` | Общий CI для всех приложений | — |
| `.github/workflows/office-paths.yml` | Проверка путей веток задач офиса `office/*` | — |
| `.github/CODEOWNERS` | Владелец защищённых путей | — |

Запуск, переменные окружения и деплой каждого приложения описаны в его README.

## Запуск с нуля

Команды выполняются из корня репозитория. Каталог приложения задают параметры `uv --directory` и `npm --prefix`, поэтому команды работают в `bash`, `zsh` и `fish` без подоболочек. `npm` вызывается с `--noproxy registry.npmjs.org`: установка пакетов не зависит от настроенного прокси npm. Переменные окружения и подробности — в README приложений: [чат](apps/chat/README.md), [офис](apps/office/README.md).

Требования:

- Python 3.12 и [uv](https://docs.astral.sh/uv/), Node 24, Docker;
- ключи Anthropic и OpenAI;
- для чата — проект Supabase с приватным bucket Storage для изображений;
- для офиса — [Ollama](https://ollama.com) с моделью `qwen3:8b`, если задачи ставятся в профиле `local` или `hybrid`;
- для выпуска из офиса — GitHub App офиса и ключ Render API. Без них офис принимает и выполняет задачи, но не открывает PR и не деплоит.

Клон:

```bash
git clone git@github.com:SlavaPush/vibe-studio.git
cd vibe-studio
```

### Чат

1. Создать `apps/chat/.env` по шаблону и заполнить ключи. Файл `.env` не попадает в Git.

   ```bash
   cp apps/chat/.env.example apps/chat/.env
   ```

2. Запустить локальную PostgreSQL. Схема БД применяется при старте API.

   ```bash
   docker compose -f apps/chat/infra/docker-compose.yml up -d
   ```

3. Проверить Storage. Скрипт создаёт приватный bucket `SUPABASE_STORAGE_BUCKET`, если его нет, и проверяет загрузку и подписанную ссылку. Без Storage текстовые ответы работают, генерация изображения завершается ошибкой.

   ```bash
   uv --directory apps/chat/api sync
   uv --directory apps/chat/api run python scripts/smoke_storage.py
   ```

4. Запустить API на порту `8001`:

   ```bash
   uv --directory apps/chat/api run uvicorn app.main:app --reload --port 8001
   ```

5. В другом терминале запустить интерфейс на `http://localhost:5173`:

   ```bash
   npm --prefix apps/chat/web ci --noproxy registry.npmjs.org
   npm --prefix apps/chat/web run dev
   ```

6. Создать код доступа в локальной БД. Скрипт печатает код один раз.

   ```bash
   uv --directory apps/chat/api run python scripts/access_codes.py add <метка>
   ```

7. Открыть `http://localhost:5173` и войти по коду.

### Офис

1. Создать `apps/office/.env` по шаблону. Заполнить ключи и коды входа `OFFICE_ACCESS_CODE` и `OFFICE_ADMIN_CODE`. Без кодов вход в панель запрещён.

   ```bash
   cp apps/office/.env.example apps/office/.env
   ```

2. Проверить в `apps/office/.env` значение `TARGET_REPO_URL=git@github.com:SlavaPush/vibe-studio.git`. Задать новый `WORKSPACE_DIR`: рабочая область прежнего репозитория `vibe-chat` не подходит.
3. Собрать образ sandbox. Без образа проверки задач завершаются ошибкой «Sandbox недоступен».

   ```bash
   docker build -t back-office-sandbox:p4 apps/office/docker/sandbox
   ```

4. Для профилей `local` и `hybrid` загрузить локальную модель:

   ```bash
   ollama pull qwen3:8b
   ```

5. Запустить API офиса на `127.0.0.1:8001`. Состояние офиса хранится в SQLite (`OFFICE_DATABASE_URL`, по умолчанию `apps/office/api/office.sqlite`).

   ```bash
   uv --directory apps/office/api sync
   uv --directory apps/office/api run --env-file ../.env uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
   ```

6. В другом терминале запустить панель:

   ```bash
   npm --prefix apps/office/web ci --noproxy registry.npmjs.org
   npm --prefix apps/office/web run dev
   ```

7. Открыть `http://localhost:5173`, а не `http://127.0.0.1:5173`: cookie сеанса имеет атрибут `Secure`, Chrome принимает такую cookie по HTTP только для `localhost`.

Настройки GitHub App и Render для выпуска — раздел «PR офиса» и «Очередь выпуска» в `apps/office/README.md`.

### Чат и офис одновременно

Панель офиса передаёт `/api` только на `127.0.0.1:8001` и занимает порт `5173` (`apps/office/web/vite.config.ts`, `strictPort`). Поэтому офис остаётся на портах по умолчанию, а чат переносится на `8002` и `5174`:

| Приложение | API | Интерфейс |
| --- | --- | --- |
| Офис | `127.0.0.1:8001` | `http://localhost:5173` |
| Чат | `127.0.0.1:8002` | `http://localhost:5174` |

1. В `apps/chat/.env` задать `ALLOWED_ORIGINS=http://localhost:5174,http://127.0.0.1:5174`. Настройки чата сначала читаются из `.env`, затем из окружения, поэтому переменная окружения не перекрывает значение из файла.
2. Запустить API чата:

   ```bash
   uv --directory apps/chat/api run uvicorn app.main:app --reload --port 8002
   ```

3. Запустить интерфейс чата:

   ```bash
   API_PROXY_TARGET=http://127.0.0.1:8002 npm --prefix apps/chat/web run dev -- --port 5174
   ```

Если запущено одно приложение, используются порты по умолчанию.

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

## Лицензии

Репозиторий и оба приложения распространяются по лицензии MIT: `LICENSE`, `apps/chat/LICENSE`, `apps/office/LICENSE`.

UI-библиотеки интерфейсов. Лицензии сверены с `package.json` установленных пакетов 25 октября 2026.

| Библиотека | Где используется | Лицензия |
| --- | --- | --- |
| shadcn/ui | чат и офис: компоненты скопированы в `web/src/components/ui/` | MIT |
| Radix Primitives (`radix-ui` 1.8.1) | чат и офис: основа компонентов shadcn/ui | MIT |
| assistant-ui (`@assistant-ui/react` 0.15.31) | чат: лента сообщений | MIT |
| lucide-react 1.61.0 | чат: иконки | ISC |
| Tailwind CSS 4.3.3 | чат и офис: стили | MIT |
| `tw-animate-css` | чат: анимации компонентов | MIT |
| `class-variance-authority` | чат и офис: варианты компонентов | Apache-2.0 |
| `cn` | чат и офис: объединение классов | MIT |

prompt-kit и Affect UI выбраны как допустимые библиотеки, но в текущем коде не используются. Исходники Affect UI Pro в репозиторий не добавляются.

## Проверка отсутствия секретов

Перед каждым commit выполнить из корня репозитория:

1. Префиксы ключей Anthropic, OpenAI и Supabase:

   ```bash
   git grep -nE 'sk-ant-|sk-proj-|sb_secret_'
   ```

   Допустимые совпадения — только шаблоны без значений: сама команда в этом `README.md`, комментарий в `apps/chat/.env.example`, регулярное выражение фильтра вывода в `apps/chat/api/app/policy.py` и тестовое значение в `apps/chat/api/tests/test_models.py`.

2. Закрытые ключи и файлы окружения. Обе команды должны вернуть пустой результат.

   ```bash
   git grep -nE 'BEGIN (RSA |EC )?PRIVATE KEY'
   git ls-files | grep -E '(^|/)\.env$|\.pem$'
   ```

3. Шаблоны `.env.example`: у ключей, строк подключения и кодов доступа значение пустое. Команда должна вывести `0` для каждого файла.

   ```bash
   grep -cE '^(ANTHROPIC_API_KEY|OPENAI_API_KEY|SUPABASE_SECRET_KEY|DATABASE_URL_SUPABASE|RENDER_API_KEY|QUOTA_SALT|OFFICE_ACCESS_CODE|OFFICE_ADMIN_CODE|VIBE_ACCESS_CODE)=.+' apps/chat/.env.example apps/office/.env.example
   ```

4. Личные данные: адреса почты в отслеживаемых файлах. Допустимы только служебные и тестовые адреса (`git@github.com`, адреса в тестовых данных и строках подключения локальной БД).

   ```bash
   git grep -hoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' | sort -u
   ```

В репозиторий не входят закрытые материалы курса: доска, транскрипции занятий и планирующие документы проекта. Ключ GitHub App офиса хранится вне репозитория (`GITHUB_APP_PRIVATE_KEY_PATH`).
