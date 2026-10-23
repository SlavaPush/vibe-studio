# vibe-chat

Публичный AI-чат: отдельные диалоги для каждого участника, проверка каждого сообщения модерацией и классификатором риска, маршрутизация запроса по намерению и сложности на подходящую модель и уровень усилий, генерация изображений, постоянная история.

Облачные модели: Anthropic (основной провайдер), OpenAI (резервный, а также модерация и изображения). Сервис развивает мультиагентный офис из репозитория [back-office](https://github.com/SlavaPush/back-office).

## Состав

| Каталог | Назначение |
| --- | --- |
| `api/` | FastAPI: сеансы, диалоги, задания, шлюз входа, маршрутизация, провайдеры, SSE |
| `web/` | React + TypeScript + Vite: интерфейс чата |
| `contracts/` | Контракт API между `web/` и `api/` (появится на этапе P1) |
| `infra/` | Dockerfile и конфигурация Render (появится на этапе P3) |
| `tests/e2e/` | Playwright (появится на этапе P2) |

Статус: этап `P0` — каркас репозитория, CI и проверки окружения. Функциональность чата ещё не реализована.

## Запуск локально

Требования: Python 3.12 и [uv](https://docs.astral.sh/uv/), Node 24, Docker.

```bash
cp .env.example .env
```

Заполнить ключи в `.env`. Файл `.env` не попадает в Git.

API:

```bash
cd api && uv sync && uv run uvicorn app.main:app --reload --port 8000
```

Интерфейс:

```bash
cd web && npm ci && npm run dev
```

Локальная PostgreSQL (адрес совпадает с `DATABASE_URL` из `.env.example`):

```bash
docker compose -f infra/docker-compose.yml up -d
```

Проверки:

```bash
cd api && uv run pytest
```

```bash
cd web && npm run build
```

Smoke-проверка облачных моделей (реальные платные вызовы; нужны ключи в `.env`; флаг `--image` добавляет генерацию одного изображения):

```bash
cd api && uv run python scripts/smoke_models.py
```

Smoke-проверка приватного Supabase Storage (bucket, загрузка, подписанная ссылка, запрет прямого доступа, удаление):

```bash
cd api && uv run python scripts/smoke_storage.py
```

## Переменные окружения

Перечень и назначение — в `.env.example`. Значения ключей не хранятся в репозитории и документации.

## Лицензия

MIT, см. `LICENSE`.
