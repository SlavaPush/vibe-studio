# vibe-chat

Публичный AI-чат: отдельные диалоги для каждого участника, проверка каждого сообщения модерацией и классификатором риска, маршрутизация запроса по намерению и сложности на подходящую модель и уровень усилий, генерация изображений, постоянная история.

Облачные модели: Anthropic (основной провайдер), OpenAI (резервный, а также модерация и изображения). Сервис развивает мультиагентный офис из репозитория [back-office](https://github.com/SlavaPush/back-office).

## Состав

| Каталог | Назначение |
| --- | --- |
| `api/` | FastAPI: сеансы, диалоги, задания, шлюз входа, маршрутизация, провайдеры, SSE |
| `web/` | React + TypeScript + Vite: интерфейс чата |
| `contracts/api.md` | Контракт API между `web/` и `api/`: операции, события, ошибки, перечисления и проверяемые примеры |
| `infra/` | Dockerfile и конфигурация Render (появится на этапе P3) |
| `tests/e2e/` | Playwright (появится на этапе P2) |

Статус: этап `P1` — контракт API, схемы Pydantic, каталог моделей (`GET /api/models`), UI-токены и каркас чата на тестовых данных. Интерфейс не обращается к серверу; сеансы, диалоги, задания и провайдеры реализуются на этапе `P2`.

## Структура `web/src`

| Каталог | Назначение |
| --- | --- |
| `api/types.ts` | Типы TypeScript, повторяющие `contracts/api.md` |
| `components/ui/` | Компоненты shadcn/ui (стиль `new-york`, палитра `neutral`), копируются командой `npx shadcn@latest add <имя>` |
| `components/chat/` | Экраны чата: вход, список диалогов, лента на assistant-ui, селектор модели, уведомления, панель сценариев |
| `store/` | Хранилище диалогов — единственный владелец сообщений; редьюсер событий `RunEvent`; адаптер `ExternalStoreRuntime` для assistant-ui |
| `mocks/` | Тестовые данные и сценарии без сети; `contract-examples.ts` создаётся из примеров контракта |
| `test/` | Настройка vitest и общие тестовые данные |

UI-токены — CSS-переменные shadcn/ui в `src/index.css`: палитра, радиус, шрифты, кольцо фокуса, цвет ошибок. Тёмная тема включается классом `.dark` по системной настройке. При `prefers-reduced-motion: reduce` анимации и переходы отключаются.

Сценарий тестовых данных выбирается параметром `?scenario=` (`chat`, `guest`, `pending`, `error`, `update`) или ссылками в нижней панели.

Файл `web/src/mocks/contract-examples.ts` не хранится в Git. Скрипт `npm run contract:examples` создаёт его из `contracts/api.md` перед `dev`, `test` и `build`. При расхождении примеров контракта с `api/types.ts` сборка не проходит.

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
cd web && npm run lint && npm run test && npm run build
```

`uv run pytest` проверяет, что каждый пример из `contracts/api.md` разбирается схемой из `api/app/schemas.py`, а перечисления совпадают. `npm run test` запускает vitest с React Testing Library.

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
