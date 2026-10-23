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
| `scripts/` | Smoke-проверки окружения и моделей |
| `workspace/` | Клон `vibe-chat` и worktree задач; в `.gitignore` |

Статус: этап `P0` — каркас репозитория, CI и проверки окружения. Граф ролей ещё не реализован.

## Профили моделей

`policies/models.yaml` задаёт модель и уровень усилий для каждой роли в профилях `local` (Ollama), `cloud` (Anthropic с резервом OpenAI) и `hybrid` (локальная модель для коротких шагов, облачная для разработчика, критика и фильтра приёма). Раздел `capabilities` описывает параметр рассуждения каждой пары провайдер/модель; поле `verified` становится `true` после реального вызова. На этапе `P0` файл — черновик, код его ещё не читает.

Локальные ограничения: одна загруженная модель Ollama (`OLLAMA_MAX_LOADED_MODELS=1`), основная модель `qwen3:8b`.

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
python3 -I scripts/smoke_ollama.py qwen3:8b
```

```bash
docker build -t back-office-sandbox docker/sandbox
```

## Лицензия

MIT, см. `LICENSE`.
