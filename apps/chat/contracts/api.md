# Контракт API `vibe-chat`

Контракт между интерфейсом `web/` и сервером `api/`. Документ — источник истины для имён полей и значений перечислений.

- Серверные схемы: `api/app/schemas.py` (Pydantic).
- Типы интерфейса: `web/src/api/types.ts` (TypeScript, повторяют схемы вручную).
- Примеры в этом файле проверяются автоматически. Перед каждым примером стоит маркер `<!-- example: Имя -->`, где `Имя` — схема из `schemas.py` и тип из `types.ts`. Перед каждым перечислением стоит маркер `<!-- enum: Имя -->`.
- `api/tests/test_contract.py` разбирает каждый пример схемой Pydantic и сверяет перечисления.
- `web/scripts/extract-contract-examples.mjs` переносит те же примеры в `web/src/mocks/contract-examples.ts`; `tsc` проверяет их типами из `types.ts`.

Изменение контракта выполняется в одном изменении с `schemas.py` и `types.ts`.

## Состояние реализации

| Операция | Этап |
| --- | --- |
| `GET /api/version`, `GET /api/models` | реализованы в `P1` |
| Сеанс, диалоги, задания, события, файлы, `GET /healthz` | реализованы в `P2` |

## Общие правила

### Формат

- Тело запросов и ответов — JSON в UTF-8. Исключение — поток событий `text/event-stream`.
- Время — строка ISO 8601 с часовым поясом UTC, например `2026-10-23T18:30:00Z`.
- Идентификаторы — строки UUID. Клиент не делает выводов из их содержимого.
- Поля, у которых нет значения, передаются как `null`. Поля не пропускаются.
- Неизвестные поля в запросе — ошибка `validation_error`.
- Идентификатор в пути, который не является UUID, — ошибка `422 validation_error`.

### Сеанс и защита от CSRF

- `POST /api/demo-session` создаёт гостевого пользователя и устанавливает cookie `vc_session` (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, срок 7 дней). Флаг `Secure` отключается только настройкой сервера `COOKIE_SECURE=false` для локальной работы.
- Ответ содержит `csrf_token`. Интерфейс передаёт его в заголовке `X-CSRF-Token` во всех запросах `POST` и `PATCH`, кроме `POST /api/demo-session`.
- Сервер проверяет заголовок `Origin` у всех изменяющих запросов.
- После перезагрузки страницы интерфейс получает `csrf_token` из `GET /api/me`.
- Восстановление истории после удаления cookie и вход с другого устройства не поддерживаются.

### Владелец объекта

Каждый запрос к диалогу, заданию и файлу проверяет владельца на сервере. Чужой объект и несуществующий объект дают одинаковый ответ `404` с кодом `not_found`.

### Ошибка

Любой ответ с кодом `4xx` или `5xx` содержит объект `ApiError`. Исключение — ответ `503` метода `GET /healthz`: он содержит объект `Health` со статусом `unavailable`. Стек сервера, ключи и внутренние пути в ответ не попадают.

| Поле | Тип | Назначение |
| --- | --- | --- |
| `code` | `ErrorCode` | Машинный код ошибки |
| `message` | строка | Понятное сообщение на русском |
| `retryable` | логическое | `true` — тот же запрос можно повторить позже |
| `operation_id` | строка | Идентификатор операции для поиска в журнале сервера |

<!-- enum: ErrorCode -->
```json
[
  "unauthorized",
  "invalid_demo_code",
  "csrf_failed",
  "not_found",
  "validation_error",
  "idempotency_conflict",
  "run_not_active",
  "rate_limited",
  "quota_exceeded",
  "provider_unavailable",
  "provider_outcome_unknown",
  "response_truncated",
  "storage_unavailable",
  "run_cancelled",
  "service_unavailable",
  "internal_error"
]
```

| Код | HTTP | `retryable` | Когда |
| --- | --- | --- | --- |
| `unauthorized` | 401 | `false` | Нет cookie сеанса или срок сеанса истёк |
| `invalid_demo_code` | 401 | `false` | Неверный код демодоступа |
| `csrf_failed` | 403 | `false` | Нет или не совпадает `X-CSRF-Token`, чужой `Origin` |
| `not_found` | 404 | `false` | Объект не существует или принадлежит другому пользователю |
| `validation_error` | 422 | `false` | Тело или параметры запроса не соответствуют схеме |
| `idempotency_conflict` | 409 | `false` | Ключ идемпотентности уже использован с другим телом запроса |
| `run_not_active` | 409 | `false` | Отмена завершённого задания |
| `rate_limited` | 429 | `true` | Слишком частые запросы или у пользователя уже есть незавершённое задание |
| `quota_exceeded` | 429 | `false` | Исчерпан бюджет пользователя или сервиса |
| `provider_unavailable` | — | `true` | Только в событии `failed`: провайдер недоступен после допустимых повторов и fallback |
| `provider_outcome_unknown` | — | `false` | Только в событии `failed`: исход платного вызова неизвестен, автоматический повтор запрещён |
| `response_truncated` | — | `false` | Только в событии `failed`: модель остановила ответ на пределе длины; полученный текст сохранён, автоматического повтора нет |
| `storage_unavailable` | 503 | `true` | Хранилище файлов недоступно; повторная генерация изображения не выполняется |
| `run_cancelled` | — | `false` | Только в событии `failed`: задание отменено пользователем |
| `service_unavailable` | 503 | `true` | Сервис перезапускается или не готов |
| `internal_error` | 500 | `false` | Непредвиденная ошибка сервера |

#### Ошибки провайдера моделей

Сервер делит ошибки платного вызова (классификатор, текстовая модель, генерация изображения) на три класса. Правило одно для всех вызовов.

| Класс | Примеры | Поведение |
| --- | --- | --- |
| Подтверждённый отказ до выполнения | соединение не установлено, HTTP 429, HTTP 529 | До двух повторов с задержкой в пределах срока задания, затем fallback на резервную модель |
| Отклонённый запрос | HTTP 400, 401, 403, 404, 413, 422 | Без повтора; fallback разрешён |
| Неизвестный исход | таймаут чтения, обрыв соединения, HTTP 500, 502, 503, 504, обрыв потока до или после первого фрагмента | Без повтора и без fallback |

- Неизвестный исход ответа модели завершает задание событием `failed` с кодом `provider_outcome_unknown` (`retryable: false`). Сохранённый частичный текст остаётся в сообщении ассистента. Интерфейс отмечает такой ответ как незавершённый.
- Неизвестный исход или ошибка проверки (модерация, классификатор) дают `check_unavailable`: основная модель не вызывается.
- Ответ, который модель остановила на пределе длины (`stop_reason: max_tokens` у Anthropic, `response.incomplete` у OpenAI), завершает задание событием `failed` с кодом `response_truncated`. Полученный текст сохраняется, повтора и fallback нет.
- `provider_unavailable` (`retryable: true`) означает, что все допустимые повторы и fallback завершились подтверждённым отказом или отклонённым запросом.
- Перезапуск сервера не продолжает повторы и fallback. Задание с начатым платным вызовом завершается `provider_outcome_unknown` или `check_unavailable` без нового вызова.

Пример ошибки:

<!-- example: ApiError -->
```json
{
  "code": "not_found",
  "message": "Диалог не найден.",
  "retryable": false,
  "operation_id": "op_01JB7Z3K8Q2M4N6P8R0T2V4X6Z"
}
```

### Перечисления

Роль сообщения:

<!-- enum: MessageRole -->
```json
["user", "assistant", "system"]
```

Тип системного сообщения. У сообщений с ролью `user` и `assistant` поле `kind` равно `null`.

<!-- enum: SystemMessageKind -->
```json
["model_switched", "refusal", "check_unavailable"]
```

Намерение, сложность и риск — результат классификатора шлюза входа:

<!-- enum: Intent -->
```json
["chat", "code", "image", "clarify"]
```

<!-- enum: Complexity -->
```json
["trivial", "standard", "complex"]
```

<!-- enum: Risk -->
```json
["none", "illegal", "internal_probe"]
```

Уровень усилий. `unsupported` — модель не принимает параметр усилий, например генерация изображения. До выбора модели поле `effort` равно `null`.

<!-- enum: Effort -->
```json
["low", "medium", "high", "unsupported"]
```

Провайдер и вид модели:

<!-- enum: Provider -->
```json
["anthropic", "openai"]
```

<!-- enum: ModelKind -->
```json
["text", "image"]
```

Идентификаторы текстовых моделей и всех моделей каталога:

<!-- enum: TextModelId -->
```json
["claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol"]
```

<!-- enum: ModelId -->
```json
["claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol", "gpt-image-2.5-flare"]
```

Выбор модели в запросе: `auto` — модель и уровень усилий выбирает маршрутизация; идентификатор текстовой модели — явная модель; `image` — генерация изображения. Уровень усилий всегда выбирает сервер.

<!-- enum: ModelChoice -->
```json
["auto", "image", "claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol"]
```

Причина переключения модели: `intent_mismatch` — выбранная модель не подходит для намерения запроса; `provider_fallback` — основной провайдер отказал, ответ выполнен резервной моделью.

<!-- enum: SwitchReason -->
```json
["intent_mismatch", "provider_fallback"]
```

Состояние задания:

<!-- enum: RunStatus -->
```json
["queued", "running", "completed", "refused", "check_unavailable", "failed", "cancelled"]
```

### Общие объекты

`Message` — сообщение диалога. Поля `effective_model` и `effort` заполнены у ответов ассистента. Поле `asset` заполнено у ответа с изображением. Системные сообщения не передаются модели в контексте.

<!-- example: Message -->
```json
{
  "id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
  "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
  "role": "assistant",
  "kind": null,
  "text": "",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "effective_model": "gpt-image-2.5-flare",
  "effort": "unsupported",
  "asset": {
    "id": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
    "mime_type": "image/png",
    "width": 1024,
    "height": 1024
  },
  "created_at": "2026-10-23T18:30:12Z"
}
```

`AssetRef` — ссылка на сохранённый файл. Адрес файла выдаёт отдельный запрос `GET /api/assets/{id}/url`.

<!-- example: AssetRef -->
```json
{
  "id": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
  "mime_type": "image/png",
  "width": 1024,
  "height": 1024
}
```

`Run` — задание на ответ:

| Поле | Назначение |
| --- | --- |
| `requested_model` | Выбор пользователя (`ModelChoice`) |
| `effective_model` | Фактическая модель; `null` до выбора |
| `effort` | Фактический уровень усилий; `null` до выбора |
| `intent`, `complexity`, `risk` | Результат классификатора; `null` до проверки |
| `switch_reason` | Причина переключения; `null`, если переключения не было |
| `assistant_message_id` | Ответ ассистента; `null`, если ответа нет |
| `error` | Ошибка завершения; `null`, если ошибки нет |

<!-- example: Run -->
```json
{
  "id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
  "status": "completed",
  "requested_model": "claude-sonnet-5-5",
  "effective_model": "gpt-image-2.5-flare",
  "effort": "unsupported",
  "intent": "image",
  "complexity": "standard",
  "risk": "none",
  "switch_reason": "intent_mismatch",
  "user_message_id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  "assistant_message_id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
  "error": null,
  "created_at": "2026-10-23T18:30:00Z",
  "updated_at": "2026-10-23T18:30:12Z"
}
```

## Сеанс

### `POST /api/demo-session`

Проверяет код демодоступа, создаёт гостевого пользователя и сеанс. Код открывает доступ, но не идентифицирует пользователя.

Запрос:

<!-- example: DemoSessionRequest -->
```json
{
  "code": "demo-2026"
}
```

Ответ `201` и cookie `vc_session`:

<!-- example: Session -->
```json
{
  "user_id": "7b0c2f3e-5d1a-4c8e-9f21-3a6d8e4b1c90",
  "expires_at": "2026-10-30T18:00:00Z",
  "csrf_token": "b4f1c9e27d3a48e6a0c5f8d1e2b3a4c5"
}
```

Ошибки: `401 invalid_demo_code`, `403 csrf_failed` (чужой `Origin`), `422 validation_error`, `429 rate_limited`.

<!-- example: ApiError -->
```json
{
  "code": "invalid_demo_code",
  "message": "Неверный код доступа.",
  "retryable": false,
  "operation_id": "op_01JB7Z4A1B2C3D4E5F6G7H8J9K"
}
```

### `GET /api/me`

Возвращает текущий сеанс. Ответ `200` — объект `Session`, как у `POST /api/demo-session`.

Ошибка без сеанса — `401 unauthorized`. Интерфейс показывает экран входа по демокоду.

<!-- example: ApiError -->
```json
{
  "code": "unauthorized",
  "message": "Сеанс не найден или истёк. Введите код доступа.",
  "retryable": false,
  "operation_id": "op_01JB7Z4M5N6P7Q8R9S0T1V2W3X"
}
```

## Диалоги

### `GET /api/conversations`

Список диалогов текущего пользователя, сначала недавно изменённые. Ответ `200`:

<!-- example: ConversationList -->
```json
{
  "items": [
    {
      "id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "title": "Кот в скафандре",
      "created_at": "2026-10-23T18:29:40Z",
      "updated_at": "2026-10-23T18:30:12Z"
    },
    {
      "id": "9a3b7c11-2e4f-4d6a-8b9c-0e1f2a3b4c5d",
      "title": "Новый диалог",
      "created_at": "2026-10-23T17:05:00Z",
      "updated_at": "2026-10-23T17:05:00Z"
    }
  ]
}
```

Ошибки: `401 unauthorized`.

### `POST /api/conversations`

Создаёт диалог. Поле `title` необязательно; без него сервер задаёт название «Новый диалог».

<!-- example: CreateConversationRequest -->
```json
{
  "title": null
}
```

Ответ `201`:

<!-- example: Conversation -->
```json
{
  "id": "9a3b7c11-2e4f-4d6a-8b9c-0e1f2a3b4c5d",
  "title": "Новый диалог",
  "created_at": "2026-10-23T17:05:00Z",
  "updated_at": "2026-10-23T17:05:00Z"
}
```

Ошибки: `401 unauthorized`, `403 csrf_failed`, `422 validation_error`.

### `PATCH /api/conversations/{id}`

Переименовывает свой диалог. Название — от 1 до 200 символов.

<!-- example: RenameConversationRequest -->
```json
{
  "title": "Кот в скафандре"
}
```

Ответ `200` — объект `Conversation` с новым названием.

Ошибки: `401 unauthorized`, `403 csrf_failed`, `404 not_found`, `422 validation_error`.

<!-- example: ApiError -->
```json
{
  "code": "validation_error",
  "message": "Название должно содержать от 1 до 200 символов.",
  "retryable": false,
  "operation_id": "op_01JB7Z5Y6Z7A8B9C0D1E2F3G4H"
}
```

### `GET /api/conversations/{id}/messages`

Сообщения диалога в порядке создания, включая системные. Ответ строится из одного снимка базы данных.

- `active_run_id` — незавершённое задание диалога (`queued` или `running`), иначе `null`. Интерфейс подключается к его событиям с `seq` 1.
- Сообщения активного задания, кроме сообщения пользователя, в `items` не входят. Интерфейс получает их из событий задания. Так повторная загрузка не дублирует текст.
- `runs` — все задания диалога (`Run`) в порядке создания. По ним интерфейс показывает ошибку последнего задания (`failed`, `cancelled`) и отмечает ответ ассистента завершившегося с ошибкой задания как незавершённый.

Ответ `200`:

<!-- example: MessageList -->
```json
{
  "items": [
    {
      "id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "role": "user",
      "kind": null,
      "text": "Нарисуй кота в скафандре",
      "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "effective_model": null,
      "effort": null,
      "asset": null,
      "created_at": "2026-10-23T18:30:00Z"
    },
    {
      "id": "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "role": "system",
      "kind": "model_switched",
      "text": "Запрос выполнен моделью gpt-image-2.5-flare, потому что выбранная модель не генерирует изображения.",
      "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "effective_model": null,
      "effort": null,
      "asset": null,
      "created_at": "2026-10-23T18:30:02Z"
    },
    {
      "id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "role": "assistant",
      "kind": null,
      "text": "",
      "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "effective_model": "gpt-image-2.5-flare",
      "effort": "unsupported",
      "asset": {
        "id": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
        "mime_type": "image/png",
        "width": 1024,
        "height": 1024
      },
      "created_at": "2026-10-23T18:30:12Z"
    }
  ],
  "active_run_id": null,
  "runs": [
    {
      "id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "status": "completed",
      "requested_model": "claude-sonnet-5-5",
      "effective_model": "gpt-image-2.5-flare",
      "effort": "unsupported",
      "intent": "image",
      "complexity": "standard",
      "risk": "none",
      "switch_reason": "intent_mismatch",
      "user_message_id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
      "assistant_message_id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
      "error": null,
      "created_at": "2026-10-23T18:30:00Z",
      "updated_at": "2026-10-23T18:30:12Z"
    }
  ]
}
```

Ошибки: `401 unauthorized`, `404 not_found`.

## Запросы к модели

### `POST /api/conversations/{id}/runs`

Сохраняет сообщение пользователя и создаёт задание. Ответ модели передаётся событиями (`GET /api/runs/{id}/events`), а не в ответе на этот запрос.

| Поле | Назначение |
| --- | --- |
| `text` | Текст сообщения, от 1 до 8000 символов |
| `model` | `ModelChoice` |
| `idempotency_key` | Ключ, созданный интерфейсом для этой отправки (UUID), от 8 до 128 символов |

<!-- example: CreateRunRequest -->
```json
{
  "text": "Нарисуй кота в скафандре",
  "model": "claude-sonnet-5-5",
  "idempotency_key": "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d"
}
```

Ответ `202` — объект `Run` в состоянии `queued`:

<!-- example: Run -->
```json
{
  "id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
  "status": "queued",
  "requested_model": "claude-sonnet-5-5",
  "effective_model": null,
  "effort": null,
  "intent": null,
  "complexity": null,
  "risk": null,
  "switch_reason": null,
  "user_message_id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  "assistant_message_id": null,
  "error": null,
  "created_at": "2026-10-23T18:30:00Z",
  "updated_at": "2026-10-23T18:30:00Z"
}
```

Повтор с тем же `idempotency_key` и тем же телом возвращает `200` и то же задание. Второе платное задание не создаётся. Тот же ключ с другим телом даёт `409 idempotency_conflict`.

У пользователя может быть только одно незавершённое задание. Новый запрос с другим ключом во время активного задания даёт `429 rate_limited` с сообщением «Дождитесь завершения текущего ответа.».

Если пользователь не переименовывал диалог, первое сообщение задаёт его название (первые 60 символов).

Ошибки: `401 unauthorized`, `403 csrf_failed`, `404 not_found`, `409 idempotency_conflict`, `422 validation_error`, `429 rate_limited`, `429 quota_exceeded`, `503 service_unavailable`.

<!-- example: ApiError -->
```json
{
  "code": "quota_exceeded",
  "message": "Лимит запросов демоверсии исчерпан.",
  "retryable": false,
  "operation_id": "op_01JB7Z6J7K8L9M0N1P2Q3R4S5T"
}
```

### `GET /api/runs/{id}`

Текущее состояние своего задания. Ответ `200` — объект `Run` (пример с `status: completed` — в разделе «Общие объекты»).

Ошибки: `401 unauthorized`, `404 not_found`.

### `POST /api/runs/{id}/cancel`

Запрашивает отмену своего задания. Тело запроса пустое. Ответ `202` — объект `Run`. Отмена завершается событием `failed` с кодом `run_cancelled` и состоянием задания `cancelled`. Уже начатый платный вызов провайдера может быть учтён в расходе.

Ошибки: `401 unauthorized`, `403 csrf_failed`, `404 not_found`, `409 run_not_active`.

<!-- example: ApiError -->
```json
{
  "code": "run_not_active",
  "message": "Задание уже завершено.",
  "retryable": false,
  "operation_id": "op_01JB7Z7U8V9W0X1Y2Z3A4B5C6D"
}
```

### `GET /api/runs/{id}/events`

Поток событий задания в формате Server-Sent Events (`Content-Type: text/event-stream`).

- Каждое событие передаётся тремя строками: `id` — значение `seq`, `event` — значение `type`, `data` — объект события в JSON.
- Повторное подключение: браузер передаёт заголовок `Last-Event-ID`; вместо него можно указать параметр `?after_seq=N`. Сервер отправляет сохранённые события с `seq` больше `N`, затем новые.
- Сервер закрывает поток после завершающего события: `completed`, `refusal`, `check_unavailable` или `failed`. Если задание уже завершено и событий после `N` нет, поток закрывается сразу.
- Каждые 15 секунд без событий сервер отправляет строку комментария `: ping`.
- Закрытие вкладки или обрыв потока не создаёт новое задание и не отменяет текущее.
- `seq` начинается с `1` внутри каждого задания. Клиент устраняет повторы по паре `run_id` и `seq`.

Пример передачи:

```text
id: 3
event: model_switched
data: {"event_id":"e3", "run_id":"0b8d…", "seq":3, "type":"model_switched", ...}
```

Ошибки до начала потока: `401 unauthorized`, `404 not_found` (тело — `ApiError`).

#### Событие

Общие поля каждого события:

| Поле | Тип | Назначение |
| --- | --- | --- |
| `event_id` | строка | Уникальный идентификатор события |
| `run_id` | строка | Задание |
| `seq` | целое ≥ 1 | Порядковый номер внутри задания |
| `type` | `RunEventType` | Тип события |
| `timestamp` | время | Момент создания события |
| `payload` | объект | Данные, зависящие от `type` |

<!-- enum: RunEventType -->
```json
[
  "accepted",
  "checks_passed",
  "refusal",
  "check_unavailable",
  "model_selected",
  "model_switched",
  "text_delta",
  "asset_ready",
  "completed",
  "failed"
]
```

| `type` | `payload` | Смысл |
| --- | --- | --- |
| `accepted` | `user_message_id`, `requested_model` | Задание принято исполнителем |
| `checks_passed` | `intent`, `complexity`, `risk` | Модерация и классификатор пропустили запрос |
| `refusal` | `message` (системное сообщение `refusal`) | Запрос отклонён шлюзом входа; основная модель не вызывалась. Завершающее событие |
| `check_unavailable` | `message` (системное сообщение `check_unavailable`) | Проверка запроса недоступна; основная модель не вызывалась. Завершающее событие |
| `model_selected` | `assistant_message_id`, `provider`, `effective_model`, `effort` | Выбраны модель и уровень усилий; создан ответ ассистента |
| `model_switched` | `requested_model`, `effective_model`, `switch_reason`, `message` (системное сообщение `model_switched`) | Запрос выполняется не той моделью, которую выбрал пользователь. Выбор в селекторе не сбрасывается |
| `text_delta` | `message_id`, `delta` | Фрагмент текста ответа |
| `asset_ready` | `message_id`, `asset` | Изображение сохранено |
| `completed` | `message` (итоговый ответ ассистента) | Ответ завершён. Завершающее событие |
| `failed` | `error` (`ApiError`) | Задание завершилось ошибкой или отменой. Завершающее событие |

Последовательности:

- Запрос изображения при выбранной текстовой модели: `accepted` → `checks_passed` → `model_switched` → `model_selected` → `asset_ready` → `completed`.
- Текстовый ответ: `accepted` → `checks_passed` → `model_selected` → `text_delta` … → `completed`.
- Уточнение (`intent: clarify`): `accepted` → `checks_passed` → `completed`. Основная модель не вызывается. Ответ ассистента содержит фиксированный уточняющий вопрос; `effective_model` и `effort` равны `null`.
- Fallback на резервную модель до первого фрагмента текста: `accepted` → `checks_passed` → `model_selected` → `model_switched` (`provider_fallback`) → `model_selected` (тот же `assistant_message_id`) → `text_delta` … → `completed`.
- Отказ шлюза входа: `accepted` → `refusal`. Проверка недоступна: `accepted` → `check_unavailable`.
- Текст сохраняется фрагментами не чаще раза в 300 мс. Поток отдаёт только сохранённые события, поэтому повторное подключение воспроизводит ту же последовательность.

Примеры событий:

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000001",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 1,
  "type": "accepted",
  "timestamp": "2026-10-23T18:30:00Z",
  "payload": {
    "user_message_id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
    "requested_model": "claude-sonnet-5-5"
  }
}
```

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000002",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 2,
  "type": "checks_passed",
  "timestamp": "2026-10-23T18:30:01Z",
  "payload": {
    "intent": "image",
    "complexity": "standard",
    "risk": "none"
  }
}
```

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000003",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 3,
  "type": "model_switched",
  "timestamp": "2026-10-23T18:30:02Z",
  "payload": {
    "requested_model": "claude-sonnet-5-5",
    "effective_model": "gpt-image-2.5-flare",
    "switch_reason": "intent_mismatch",
    "message": {
      "id": "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "role": "system",
      "kind": "model_switched",
      "text": "Запрос выполнен моделью gpt-image-2.5-flare, потому что выбранная модель не генерирует изображения.",
      "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "effective_model": null,
      "effort": null,
      "asset": null,
      "created_at": "2026-10-23T18:30:02Z"
    }
  }
}
```

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000004",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 4,
  "type": "model_selected",
  "timestamp": "2026-10-23T18:30:02Z",
  "payload": {
    "assistant_message_id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
    "provider": "openai",
    "effective_model": "gpt-image-2.5-flare",
    "effort": "unsupported"
  }
}
```

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000005",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 5,
  "type": "asset_ready",
  "timestamp": "2026-10-23T18:30:11Z",
  "payload": {
    "message_id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
    "asset": {
      "id": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
      "mime_type": "image/png",
      "width": 1024,
      "height": 1024
    }
  }
}
```

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-1111-4a1a-9a1a-000000000006",
  "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
  "seq": 6,
  "type": "completed",
  "timestamp": "2026-10-23T18:30:12Z",
  "payload": {
    "message": {
      "id": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
      "conversation_id": "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37",
      "role": "assistant",
      "kind": null,
      "text": "",
      "run_id": "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47",
      "effective_model": "gpt-image-2.5-flare",
      "effort": "unsupported",
      "asset": {
        "id": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
        "mime_type": "image/png",
        "width": 1024,
        "height": 1024
      },
      "created_at": "2026-10-23T18:30:12Z"
    }
  }
}
```

Фрагмент текстового ответа другого задания:

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-2222-4a1a-9a1a-000000000004",
  "run_id": "5d6e7f80-9a1b-4c2d-8e3f-4a5b6c7d8e9f",
  "seq": 4,
  "type": "text_delta",
  "timestamp": "2026-10-23T18:40:03Z",
  "payload": {
    "message_id": "6f7a8b9c-0d1e-4f2a-9b3c-4d5e6f7a8b9c",
    "delta": "Скафандр защищает от вакуума"
  }
}
```

Отказ шлюза входа:

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-3333-4a1a-9a1a-000000000002",
  "run_id": "7e8f9a0b-1c2d-4e3f-8a4b-5c6d7e8f9a0b",
  "seq": 2,
  "type": "refusal",
  "timestamp": "2026-10-23T18:45:01Z",
  "payload": {
    "message": {
      "id": "8a9b0c1d-2e3f-4a4b-9c5d-6e7f8a9b0c1d",
      "conversation_id": "9a3b7c11-2e4f-4d6a-8b9c-0e1f2a3b4c5d",
      "role": "system",
      "kind": "refusal",
      "text": "Этот запрос нельзя выполнить: он нарушает правила сервиса.",
      "run_id": "7e8f9a0b-1c2d-4e3f-8a4b-5c6d7e8f9a0b",
      "effective_model": null,
      "effort": null,
      "asset": null,
      "created_at": "2026-10-23T18:45:01Z"
    }
  }
}
```

Проверка запроса недоступна:

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-4444-4a1a-9a1a-000000000002",
  "run_id": "9b0c1d2e-3f4a-4b5c-8d6e-7f8a9b0c1d2e",
  "seq": 2,
  "type": "check_unavailable",
  "timestamp": "2026-10-23T18:50:06Z",
  "payload": {
    "message": {
      "id": "0c1d2e3f-4a5b-4c6d-9e7f-8a9b0c1d2e3f",
      "conversation_id": "9a3b7c11-2e4f-4d6a-8b9c-0e1f2a3b4c5d",
      "role": "system",
      "kind": "check_unavailable",
      "text": "Проверка запроса недоступна, повторите позже.",
      "run_id": "9b0c1d2e-3f4a-4b5c-8d6e-7f8a9b0c1d2e",
      "effective_model": null,
      "effort": null,
      "asset": null,
      "created_at": "2026-10-23T18:50:06Z"
    }
  }
}
```

Ошибка провайдера после fallback:

<!-- example: RunEvent -->
```json
{
  "event_id": "8c1a0e5d-5555-4a1a-9a1a-000000000005",
  "run_id": "1d2e3f4a-5b6c-4d7e-8f8a-9b0c1d2e3f4a",
  "seq": 5,
  "type": "failed",
  "timestamp": "2026-10-23T18:55:20Z",
  "payload": {
    "error": {
      "code": "provider_unavailable",
      "message": "Модели временно недоступны. Повторите запрос позже.",
      "retryable": true,
      "operation_id": "op_01JB7Z8E9F0G1H2J3K4L5M6N7P"
    }
  }
}
```

## Файлы

### `GET /api/assets/{id}/url`

Краткоживущая подписанная ссылка на свой файл из приватного хранилища. Ссылка действует 10 минут, до `expires_at`; после этого интерфейс запрашивает новую. Если при генерации хранилище было недоступно, сервер сначала загружает сохранённое изображение; повторная генерация не выполняется. Ответ `200`:

<!-- example: AssetUrl -->
```json
{
  "url": "https://storage.example.com/object/sign/assets/4f5a6b7c.png?token=signed",
  "expires_at": "2026-10-23T18:40:12Z"
}
```

Ошибки: `401 unauthorized`, `404 not_found`, `503 storage_unavailable`.

<!-- example: ApiError -->
```json
{
  "code": "storage_unavailable",
  "message": "Хранилище файлов временно недоступно.",
  "retryable": true,
  "operation_id": "op_01JB7Z9Q0R1S2T3V4W5X6Y7Z8A"
}
```

## Модели

### `GET /api/models`

Каталог профилей моделей для селектора. Не требует сеанса. Не содержит ключей, адресов провайдеров и внутренних настроек. Профиль с `selectable: false` нельзя указать в `model` напрямую: генерация изображения выбирается значением `image`. Ответ `200`:

<!-- example: ModelCatalog -->
```json
{
  "items": [
    {
      "id": "claude-haiku-5-5",
      "provider": "anthropic",
      "label": "Claude Haiku 5.5",
      "kind": "text",
      "selectable": true
    },
    {
      "id": "claude-sonnet-5-5",
      "provider": "anthropic",
      "label": "Claude Sonnet 5.5",
      "kind": "text",
      "selectable": true
    },
    {
      "id": "gpt-6-luna",
      "provider": "openai",
      "label": "GPT-6 Luna",
      "kind": "text",
      "selectable": true
    },
    {
      "id": "gpt-6.1-sol",
      "provider": "openai",
      "label": "GPT-6.1 Sol",
      "kind": "text",
      "selectable": true
    },
    {
      "id": "gpt-image-2.5-flare",
      "provider": "openai",
      "label": "GPT Image 2.5 Flare",
      "kind": "image",
      "selectable": false
    }
  ]
}
```

## Версия и готовность

### `GET /api/version`

Версия развёрнутой сборки. Не требует сеанса. Ответ передаётся с заголовком `Cache-Control: no-store`. Интерфейс сравнивает её с собственной версией при возвращении на вкладку и с умеренным интервалом. При различии показывает уведомление «Доступна новая версия»; страницу обновляет пользователь. Ответ `200`:

<!-- example: VersionInfo -->
```json
{
  "version": "4bd1e4f"
}
```

### `GET /healthz`

Готовность экземпляра к приёму запросов: база данных доступна, схема применена, восстановление заданий завершено, исполнитель заданий запущен. Не требует сеанса. Ответ `200`:

<!-- example: Health -->
```json
{
  "status": "ok"
}
```

Ответ `503` при неготовности:

<!-- example: Health -->
```json
{
  "status": "unavailable"
}
```

<!-- enum: HealthStatus -->
```json
["ok", "unavailable"]
```
