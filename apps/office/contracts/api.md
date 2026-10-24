# Контракт API панели офиса

Документ описывает HTTP API между панелью `web/` и API офиса `api/`. Серверные схемы находятся в `api/app/schemas.py`, типы панели — в `web/src/api/types.ts`. Имена полей и значения перечислений в обоих местах совпадают с этим документом.

С подэтапа `P4.1` API работает с БД офиса и проверяет сеанс и права на сервере. `GET /api/tasks/{id}/diff` реализуется на подэтапе `P4.2`. Панель переходит на этот API на подэтапе `P4.4`.

## Общие правила

- Базовый путь — `/api`. Тела запросов и ответов — JSON в UTF-8, кроме diff, артефактов и потоков SSE.
- Время передаётся в ISO 8601 UTC, например `2026-10-23T10:15:00Z`.
- Идентификаторы — строки с префиксом сущности: `prt_` (участник), `tsk_` (задача), `rel_` (кандидат выпуска), `dec_` (решение). Исключение: `event_id` — целое число.
- Сеанс передаётся cookie `office_session` с атрибутами `HttpOnly`, `Secure`, `SameSite=Lax`. Сервер хранит только хеш токена сеанса. Запрос без действующего сеанса получает `401 unauthorized`; исключения — `POST /api/office-session` и `DELETE /api/office-session`.
- Роль участника определяется кодом доступа при создании сеанса. Ни одно поле запроса не задаёт роль и не разрешает действие.
- Неизвестные поля в теле запроса отклоняются с ошибкой `422 validation_error`.

### Примеры

Каждый пример JSON в этом документе стоит после маркера `<!-- example: <Модель> -->`. `<Модель>` — имя Pydantic-модели из `api/app/schemas.py`; суффикс `[]` означает массив таких моделей. Тест `api/tests/test_contract.py` разбирает каждый пример своей моделью. Скрипт `web/scripts/extract-contract-examples.mjs` извлекает те же примеры в тестовые данные панели.

### Версия объекта и идемпотентность

У задачи и кандидата выпуска есть поле `version`. Сервер увеличивает его при каждом изменении объекта. Запрос решения передаёт `expected_version` — версию, которую видел участник. Если версия объекта на сервере другая, решение не записывается, ответ — `409 version_mismatch`.

Запрос решения передаёт `idempotency_key` — строку от 8 до 64 символов, уникальную для попытки участника. Повтор запроса с тем же ключом и тем же телом возвращает ранее записанное решение с кодом `200`. Тот же ключ с другим телом получает `409 idempotency_conflict`. Изменение задачи или кандидата аннулирует ожидание решения для старой версии.

## Перечисления

| Перечисление | Значения |
| --- | --- |
| `Role` | `participant`, `admin` |
| `TaskStatus` | `rejected`, `queued`, `awaiting_spec_approval`, `developing`, `reviewing`, `awaiting_integration`, `awaiting_release`, `deploying`, `released`, `failed`, `cancelled`, `needs_attention` |
| `ModelProfile` | `local`, `cloud`, `hybrid` |
| `AgentRole` | `supervisor`, `researcher`, `developer`, `qa`, `critic`, `pr_reviewer` |
| `TaskEventType` | `task_created`, `task_rejected`, `status_changed`, `role_started`, `role_finished`, `tool_called`, `artifact_ready`, `decision_required`, `decision_recorded`, `check_result`, `release_candidate`, `deploy_status`, `pr_opened`, `checks_status`, `pr_closed`, `operation_unknown`, `pr_review`, `merge_started`, `pr_merged` |
| `FailureKind` | `checks_failed`, `pr_rejected` |
| `TaskDecision` | `approve_spec`, `cancel`, `retry` |
| `ReleaseDecision` | `approve_release`, `rollback` |
| `ReleaseKind` | `release`, `rollback` |
| `ReleaseStatus` | `pending`, `approved`, `deploying`, `released`, `failed`, `rolled_back`, `revoked` |

Порядок статусов задачи фиксирован:

<!-- example: TaskStatus[] -->
```json
[
  "rejected",
  "queued",
  "awaiting_spec_approval",
  "developing",
  "reviewing",
  "awaiting_integration",
  "awaiting_release",
  "deploying",
  "released",
  "failed",
  "cancelled",
  "needs_attention"
]
```

Порядок типов событий фиксирован:

<!-- example: TaskEventType[] -->
```json
[
  "task_created",
  "task_rejected",
  "status_changed",
  "role_started",
  "role_finished",
  "tool_called",
  "artifact_ready",
  "decision_required",
  "decision_recorded",
  "check_result",
  "release_candidate",
  "deploy_status",
  "pr_opened",
  "checks_status",
  "pr_closed",
  "operation_unknown",
  "pr_review",
  "merge_started",
  "pr_merged"
]
```

### Статусы задачи и слоты

| Статус | Значение | Занимает слот |
| --- | --- | --- |
| `rejected` | Фильтр приёма отклонил задачу; причина в `rejection_reason` | нет |
| `queued` | Задача принята и ждёт свободного слота | нет |
| `awaiting_spec_approval` | Задача заняла слот. Пока `spec = null`, Supervisor готовит требования; после этого требования ждут решения владельца `approve_spec` | да |
| `developing` | Разработчик изменяет код в worktree задачи | да |
| `reviewing` | QA и критик проверяют зафиксированную версию | да |
| `awaiting_integration` | Ветка проверена. Офис отправляет её в `vibe-studio`, открывает PR от GitHub App и ждёт обязательных проверок PR; сводка проверок — в поле `checks`. После успешных проверок агент ревью PR принимает решение (поле `pr_review`); при `approve` офис сливает PR (squash) и проверяет итоговый commit `merge_sha`: обязательные проверки `push` и критический E2E | нет |
| `awaiting_release` | PR слит, итоговый commit прошёл проверки и критический E2E; кандидат выпуска ждёт очереди выпуска | нет |
| `deploying` | Выполняется деплой кандидата | нет |
| `released` | Выпуск опубликован | нет |
| `failed` | Задача завершилась ошибкой; результат сохранён. Причина выпуска — в `failure_kind` | нет |
| `cancelled` | Владелец отменил задачу; открытый PR задачи офис закрывает | нет |
| `needs_attention` | Исчерпан бюджет исправлений, исход внешней операции неизвестен или агент ревью PR не дал действительного решения (невалидный ответ, ошибка провайдера, исчерпанный предел); PR остаётся открытым | нет |

Слот занимают статусы от `awaiting_spec_approval` до `reviewing` включительно. Всего слотов три. Задача в `queued` захватывает слот, когда он освобождается. Переход задачи в `awaiting_integration` освобождает слот. Worktree задачи сохраняется до её закрытия (`cancelled`, `released`).

## Ошибки

Любой ответ с кодом `4xx` или `5xx` содержит модель `ApiError`. Стек сервера, ключи и внутренние пути клиенту не возвращаются.

| Поле | Тип | Описание |
| --- | --- | --- |
| `code` | string | Машинный код ошибки из таблицы ниже |
| `message` | string | Понятное сообщение на русском |
| `retryable` | boolean | Можно ли повторить тот же запрос без изменений |
| `operation_id` | string | Идентификатор операции для поиска в журнале сервера |

| HTTP | `code` | `retryable` | Когда возникает |
| --- | --- | --- | --- |
| 400 | `invalid_artifact_name` | false | Имя артефакта содержит `/`, `..` или недопустимые символы |
| 401 | `unauthorized` | false | Нет действующего сеанса |
| 401 | `invalid_access_code` | false | Неверный код доступа офиса |
| 403 | `forbidden_not_owner` | false | Решение по задаче принимает не её владелец |
| 403 | `forbidden_role` | false | Действие выпуска запрашивает участник без роли `admin` |
| 403 | `forbidden_origin` | false | Заголовок `Host` любого запроса или `Origin` изменяющего запроса (`POST`, `PUT`, `PATCH`, `DELETE`) не входит в разрешённый список; отсутствующий `Origin` и `Origin: null` у изменяющего запроса также отклоняются |
| 404 | `not_found` | false | Объект не существует или артефакт не принадлежит задаче |
| 409 | `version_mismatch` | false | `expected_version` не совпадает с текущей версией объекта |
| 409 | `decision_not_allowed` | false | Решение недопустимо в текущем статусе объекта |
| 409 | `idempotency_conflict` | false | Ключ идемпотентности уже использован с другим телом запроса |
| 413 | `diff_too_large` | false | Diff больше 1 МиБ; список файлов доступен в `changed_files` задачи |
| 422 | `validation_error` | false | Тело запроса не соответствует схеме |
| 429 | `rate_limited` | true | Превышен предел запросов; заголовок `Retry-After` содержит паузу в секундах |
| 500 | `internal_error` | false | Внутренняя ошибка; повтор только после проверки состояния объекта |

<!-- example: ApiError -->
```json
{
  "code": "unauthorized",
  "message": "Сеанс не найден или истёк. Войдите по коду офиса.",
  "retryable": false,
  "operation_id": "op_7f3c2a91"
}
```

## Права

| Действие | `participant` | `admin` |
| --- | --- | --- |
| Читать задачи, события, diff, проверки, артефакты, слоты, выпуски | да | да |
| Создать задачу | да | да |
| `approve_spec`, `cancel`, `retry` своей задачи | да | да |
| `approve_spec`, `cancel`, `retry` чужой задачи | нет, `403 forbidden_not_owner` | нет, `403 forbidden_not_owner` |
| `approve_release`, `rollback` | нет, `403 forbidden_role` | да |

Решения по задаче доступны в статусах:

| Решение | Допустимые статусы задачи | Дополнительное условие |
| --- | --- | --- |
| `approve_spec` | `awaiting_spec_approval` | Требования готовы: `spec` не равно `null` |
| `cancel` | `queued`, `awaiting_spec_approval`, `developing`, `reviewing`, `awaiting_integration`, `needs_attention` | PR задачи не слит (`merge_sha = null`) и слияние не выполняется (`merge_pending = false`) |
| `retry` | `failed`, `needs_attention` | Бюджет обращений не исчерпан: `usage.model_calls` меньше `usage.model_calls_limit` (на этапе интеграции — см. ниже); для `failed` поле `failure_kind` не равно `checks_failed` и `pr_rejected` |

`retry` возвращает задачу в `queued`; задача заново проходит очередь слотов. `usage` и `fix_cycles` при этом сохраняются: пределы относятся к задаче, а не к попытке. Исключение: `retry` задачи в `needs_attention` с `integration_started = true` (есть операции выпуска — push ветки, создание, слияние или закрытие PR, обращение к агенту ревью — или заполнены `head_sha`, `pr_number` или `merge_sha`) возвращает её в `awaiting_integration`. Офис сначала сверяет незавершённые операции с GitHub и продолжает с места остановки; если исход по-прежнему не установлен, задача снова получает `needs_attention`. Для такого `retry` бюджет графа (`usage.model_calls_limit`) не проверяется: чтение проверок, слияние, сверка и E2E не обращаются к моделям. Если для текущего `head_sha` нет решения агента ревью и задача ещё не слита, нужен остаток предела обращений агента ревью (`OFFICE_PR_REVIEW_CALLS` на задачу, по умолчанию 2); исчерпанный предел — `409 decision_not_allowed`.

Задача с `failure_kind` `checks_failed` (обязательные проверки PR не пройдены) или `pr_rejected` (PR отклонён агентом ревью) не повторяется: нужна новая задача.

`cancel` в `awaiting_integration` переводит задачу в `cancelled`; офис закрывает открытый PR задачи и записывает событие `pr_closed`. После слияния PR (`merge_sha` заполнен) и во время слияния `cancel` недоступен (`409 decision_not_allowed`): изменение уже в `main` или может там оказаться.

Решения по выпуску доступны в статусах:

| Решение | Условие |
| --- | --- |
| `approve_release` | Кандидат в статусе `pending` |
| `rollback` | Кандидат в статусе `released` и `is_published = true`; создаётся кандидат вида `rollback` на версию `expected_previous_sha` этого выпуска |

Решение в другом статусе или при невыполненном дополнительном условии получает `409 decision_not_allowed`.

## Модели

### `Participant`

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | string | Идентификатор участника |
| `display_name` | string | Отображаемое имя, 1–40 символов |
| `role` | `Role` | Роль по коду доступа |
| `expires_at` | string (дата-время) | Окончание сеанса |

### `Task`

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | string | Идентификатор задачи |
| `title` | string | Заголовок: первая строка текста, до 80 символов |
| `text` | string | Текст задачи участника |
| `status` | `TaskStatus` | Текущий статус |
| `profile` | `ModelProfile` | Профиль моделей, выбранный при постановке |
| `owner_id` | string | Идентификатор владельца |
| `owner_name` | string | Отображаемое имя владельца |
| `version` | integer | Версия объекта для `expected_version` |
| `created_at`, `updated_at` | string (дата-время) | Время создания и последнего изменения |
| `rejection_reason` | string или `null` | Причина отказа для статуса `rejected` |
| `spec` | `Spec` или `null` | Требования, когда Supervisor их подготовил |
| `branch` | string или `null` | Ветка `office/<task_id>` в `vibe-studio` |
| `base_sha` | string или `null` | Commit, от которого создан worktree |
| `pr_url` | string или `null` | Адрес PR в `vibe-studio` |
| `pr_number` | integer или `null` | Номер PR в `vibe-studio` |
| `head_sha` | string или `null` | Commit ветки `office/<task_id>`, отправленный в `vibe-studio` |
| `failure_kind` | `FailureKind` или `null` | Причина `failed` на этапе выпуска: `checks_failed` — обязательные проверки PR или итогового commit (в том числе критический E2E) не пройдены, `pr_rejected` — PR отклонён агентом ревью; `null` для остальных причин |
| `checks` | `PullRequestChecks` или `null` | Последняя сводка обязательных проверок: до слияния — проверки PR для `head_sha`, после слияния — проверки события `push` итогового commit `merge_sha` (поле `checks.head_sha` указывает проверенный commit); `null`, пока PR не проверялся и сразу после слияния |
| `pr_review` | `PullRequestReview` или `null` | Последнее решение агента ревью PR; `null`, пока агент не вызывался. Решение действительно только для своего `head_sha` |
| `merge_sha` | string или `null` | Итоговый commit squash-слияния PR в `main`; `null`, пока PR не слит |
| `merge_pending` | boolean | Слияние PR выполняется или его исход не установлен (операция `merge_pr` в статусе `started` или `unknown`); `cancel` недоступен |
| `integration_started` | boolean | Задача на этапе интеграции: есть операции журнала выпуска или заполнены `head_sha`, `pr_number`, `merge_sha`. `retry` такой задачи из `needs_attention` возвращает её в `awaiting_integration` без проверки бюджета графа |
| `changed_files` | string[] | Изменённые файлы относительно `base_sha` |
| `fix_cycles` | integer | Выполненные циклы исправления, 0–2 |
| `usage` | `Usage` | Расход задачи |

`Spec`: `version` (integer), `spec_hash` (string), `summary` (string), `acceptance_criteria` (string[]), `sources` (массив `{title, url}`).

`Usage`: `model_calls` (integer), `model_calls_limit` (integer, 30), `input_tokens` (integer), `output_tokens` (integer).

`PullRequestReview`: `id` (string), `head_sha` (string — head PR, для которого принято решение), `verdict` (`approve` или `reject`), `reasons` (string — обоснование), `required_changes` (string[] — что изменить; при `reject` не пуст), `checklist` (массив `{item, ok, note}`: `item` — пункт чек-листа `paths`, `network`, `external_navigation`, `inputs`, `system_messages`, `sent_text`, `third_party_code`; `ok` — boolean; `note` — string), `created_at` (дата-время).

`PullRequestChecks`: `head_sha` (string — проверенный commit), `checks` (массив `PullRequestCheck`). `PullRequestCheck`: `name` (string — имя обязательной проверки из ruleset `main`), `status` (string: `missing` — засчитанного запуска нет, `queued`, `in_progress`, `completed` и другие статусы job GitHub Actions), `conclusion` (string или `null` — итог job для `completed`: `success`, `failure`, `cancelled`, `timed_out`, `action_required`, `skipped` и другие), `url` (string или `null` — страница job). Проверка пройдена только при `conclusion = success`.

### `TaskEvent`

| Поле | Тип | Описание |
| --- | --- | --- |
| `event_id` | integer | Глобально возрастающий номер события; используется как `id` в SSE |
| `task_id` | string | Задача события |
| `seq` | integer | Номер события внутри задачи, начиная с 1, без пропусков |
| `type` | `TaskEventType` | Тип события |
| `timestamp` | string (дата-время) | Время события |
| `role` | `AgentRole` или `null` | Роль агента, если событие создано ролью |
| `tool` | string или `null` | Имя инструмента, если событие связано с вызовом инструмента |
| `artifact` | string или `null` | Имя артефакта для `GET /api/tasks/{id}/artifacts/{name}` |
| `payload` | object | Данные события по типу |

| `type` | `payload` |
| --- | --- |
| `task_created` | `title`, `profile` |
| `task_rejected` | `reason` |
| `status_changed` | `from`, `to` — статусы задачи |
| `role_started` | `step` — шаг роли |
| `role_finished` | `step`, `outcome` (`ok`, `blocked`, `error`), `summary` |
| `tool_called` | `arguments` — краткое описание аргументов, `result` (`ok`, `denied`, `error`) |
| `artifact_ready` | `name`, `size_bytes` |
| `decision_required` | `decision`, `expected_version`, при выпуске также `release_id` |
| `decision_recorded` | `decision`, `decision_id`, `participant_id` |
| `check_result` | `command`, `exit_code`, `duration_ms` |
| `release_candidate` | `release_id`, `commit_sha` |
| `deploy_status` | `release_id`, `render_deploy_id`, `render_status` |
| `pr_opened` | `pr_number`, `pr_url` — PR задачи создан или найден |
| `checks_status` | `head_sha`, `checks` — новая сводка обязательных проверок PR или итогового commit (модель `PullRequestChecks`) |
| `pr_closed` | `pr_number` — офис закрыл PR задачи |
| `operation_unknown` | `kind` (`push`, `create_pr`, `close_pr`, `merge_pr`), `pr_number`, `reason` — исход внешней операции закрытой задачи не установлен, офис её больше не повторяет |
| `pr_review` | `review_id`, `head_sha`, `verdict` (`approve`, `reject`) — решение агента ревью PR сохранено; роль `pr_reviewer` |
| `merge_started` | `pr_number`, `head_sha` — офис записал операцию слияния и отправляет запрос; с этого момента `merge_pending = true`, `cancel` недоступен |
| `pr_merged` | `pr_number`, `merge_sha` — офис слил PR задачи в `main` |

### `CheckReport`

| Поле | Тип | Описание |
| --- | --- | --- |
| `task_id` | string | Задача |
| `commit_sha` | string или `null` | Проверенная версия кода; `null`, пока проверок не было и `base_sha` задачи равен `null` |
| `spec_hash` | string или `null` | Хеш требований, по которым выполнялись проверки; `null`, пока проверок не было |
| `suite_hash` | string или `null` | Хеш набора проверок; `null`, пока проверок не было |
| `checks` | `CheckResult[]` | Результаты команд |

`CheckResult`: `command` (string), `exit_code` (integer), `duration_ms` (integer), `artifact_urls` (string[] — адреса `GET /api/tasks/{id}/artifacts/{name}`).

### `Decision`

| Поле | Тип | Описание |
| --- | --- | --- |
| `decision_id` | string | Идентификатор решения |
| `decision` | `TaskDecision` или `ReleaseDecision` | Решение |
| `task_id` | string или `null` | Задача для решений `TaskDecision` |
| `release_id` | string или `null` | Кандидат для решений `ReleaseDecision` |
| `participant_id` | string | Кто принял решение |
| `expected_version` | integer | Версия объекта, для которой принято решение |
| `idempotency_key` | string | Ключ идемпотентности запроса |
| `recorded_at` | string (дата-время) | Время записи |

### `ReleaseCandidate`

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | string | Идентификатор кандидата |
| `task_id` | string | Задача, из которой получен кандидат |
| `kind` | `ReleaseKind` | Выпуск или откат |
| `status` | `ReleaseStatus` | Статус в очереди |
| `version` | integer | Версия объекта для `expected_version` |
| `commit_sha` | string | Итоговый commit для деплоя |
| `expected_previous_sha` | string | Опубликованная версия, относительно которой считался diff и давалось подтверждение |
| `spec_hash` | string | Хеш требований |
| `suite_hash` | string | Хеш набора проверок |
| `ci_run_ids` | string[] | Идентификаторы запусков обязательных проверок GitHub Actions для `commit_sha` |
| `pr_url` | string или `null` | PR, из которого получен commit; `null` для отката |
| `created_at` | string (дата-время) | Время создания |
| `approved_by` | string или `null` | Администратор, подтвердивший кандидата |
| `approved_at` | string (дата-время) или `null` | Время подтверждения |
| `is_published` | boolean | Версия этого кандидата опубликована сейчас |
| `deploy` | `DeployResult` или `null` | Результат деплоя |
| `pr_review_id` | string или `null` | Решение агента ревью PR (`PullRequestReview.id`) — основание выпуска; `null` для отката |
| `e2e` | `CheckReport` или `null` | Критический E2E итогового commit: `lint`, `test`, `build`, `e2e` со всеми тестами QA из `main` на `commit_sha`, с `suite_hash`; `null` для отката |

`DeployResult`: `render_deploy_id` (string), `render_status` (string, статус деплоя Render), `started_at` (дата-время), `finished_at` (дата-время или `null`), `published_sha` (string или `null` — версия из `GET /api/version` после деплоя), `error` (string или `null`).

### `Slots`

`used` (integer) — задачи в статусах, занимающих слот; `total` (integer) — всего слотов, `3`.

## Операции

### `POST /api/office-session`

Создаёт сеанс участника по коду доступа офиса и отображаемому имени. Ответ устанавливает cookie `office_session`. Роль определяется кодом: код участника даёт `participant`, код администратора — `admin`.

Запрос:

<!-- example: OfficeSessionRequest -->
```json
{
  "access_code": "OFFICE-2026",
  "display_name": "Мария"
}
```

Ответ `200`:

<!-- example: Participant -->
```json
{
  "id": "prt_maria",
  "display_name": "Мария",
  "role": "participant",
  "expires_at": "2026-10-24T09:00:00Z"
}
```

Ошибка `401`:

<!-- example: ApiError -->
```json
{
  "code": "invalid_access_code",
  "message": "Неверный код доступа офиса.",
  "retryable": false,
  "operation_id": "op_1a2b3c4d"
}
```

Ошибка `429` после серии неверных кодов:

<!-- example: ApiError -->
```json
{
  "code": "rate_limited",
  "message": "Слишком много попыток входа. Повторите через минуту.",
  "retryable": true,
  "operation_id": "op_5e6f7a8b"
}
```

Ошибки: `422 validation_error` при пустом имени или имени длиннее 40 символов.

### `DELETE /api/office-session`

Завершает текущий сеанс. Сервер удаляет запись сеанса с хешем токена из cookie `office_session`. Участник, его задачи и другие сеансы того же и других участников сохраняются.

Ответ `204` без тела. Заголовок `Set-Cookie` удаляет cookie `office_session` с теми же атрибутами: `Path=/`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=0`.

Операция идемпотентна. Запрос без cookie, с неизвестным или истёкшим токеном тоже получает `204` и заголовок, удаляющий cookie.

После выхода тот же токен получает `401 unauthorized` во всех операциях, где нужен сеанс. Открытые потоки SSE этого сеанса завершаются не позже следующего комментария `: ping`.

Ошибки: `403 forbidden_origin`, если `Origin` запроса не входит в разрешённый список.

### `GET /api/me`

Возвращает участника текущего сеанса.

Ответ `200`:

<!-- example: Participant -->
```json
{
  "id": "prt_admin",
  "display_name": "Вячеслав",
  "role": "admin",
  "expires_at": "2026-10-24T09:00:00Z"
}
```

Ошибки: `401 unauthorized` (пример в разделе «Ошибки»).

### `GET /api/tasks`

Возвращает все задачи офиса, новые первыми. Читать задачи может любой участник.

Ответ `200`:

<!-- example: Task[] -->
```json
[
  {
    "id": "tsk_103",
    "title": "Перевести backend vibe-chat на другую базу данных",
    "text": "Перевести backend vibe-chat на другую базу данных и обновить миграции.",
    "status": "rejected",
    "profile": "cloud",
    "owner_id": "prt_ivan",
    "owner_name": "Иван",
    "version": 2,
    "created_at": "2026-10-23T11:00:00Z",
    "updated_at": "2026-10-23T11:00:05Z",
    "rejection_reason": "Задача меняет backend и схему БД. Офис принимает только изменения интерфейса vibe-chat.",
    "spec": null,
    "branch": null,
    "base_sha": null,
    "pr_url": null,
    "pr_number": null,
    "head_sha": null,
    "failure_kind": null,
    "checks": null,
    "pr_review": null,
    "merge_sha": null,
    "merge_pending": false,
    "integration_started": false,
    "changed_files": [],
    "fix_cycles": 0,
    "usage": {
      "model_calls": 1,
      "model_calls_limit": 30,
      "input_tokens": 640,
      "output_tokens": 85
    }
  },
  {
    "id": "tsk_101",
    "title": "Кнопка копирования ответа ассистента",
    "text": "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена с подтверждением «Скопировано».",
    "status": "developing",
    "profile": "hybrid",
    "owner_id": "prt_maria",
    "owner_name": "Мария",
    "version": 5,
    "created_at": "2026-10-23T09:00:00Z",
    "updated_at": "2026-10-23T10:40:00Z",
    "rejection_reason": null,
    "spec": {
      "version": 1,
      "spec_hash": "sha256:4b1d9e07c2a85f36",
      "summary": "Под каждым завершённым ответом ассистента появляется кнопка «Копировать». После нажатия текст ответа попадает в буфер обмена, кнопка на 2 секунды показывает «Скопировано».",
      "acceptance_criteria": [
        "Кнопка есть только у завершённых ответов ассистента.",
        "Кнопка доступна с клавиатуры и имеет подпись для экранного диктора.",
        "При ошибке буфера обмена пользователь видит сообщение «Не удалось скопировать»."
      ],
      "sources": [
        {
          "title": "MDN: Clipboard.writeText()",
          "url": "https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText"
        }
      ]
    },
    "branch": "office/tsk_101",
    "base_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "pr_url": null,
    "pr_number": null,
    "head_sha": null,
    "failure_kind": null,
    "checks": null,
    "pr_review": null,
    "merge_sha": null,
    "merge_pending": false,
    "integration_started": false,
    "changed_files": [
      "web/src/components/chat/MessageActions.tsx",
      "web/src/components/chat/AssistantMessage.tsx"
    ],
    "fix_cycles": 0,
    "usage": {
      "model_calls": 9,
      "model_calls_limit": 30,
      "input_tokens": 48210,
      "output_tokens": 9120
    }
  },
  {
    "id": "tsk_102",
    "title": "Переключатель тёмной темы в настройках",
    "text": "Добавить в настройки чата переключатель светлой и тёмной темы с сохранением выбора в браузере.",
    "status": "awaiting_release",
    "profile": "cloud",
    "owner_id": "prt_admin",
    "owner_name": "Вячеслав",
    "version": 14,
    "created_at": "2026-10-23T07:30:00Z",
    "updated_at": "2026-10-23T10:50:00Z",
    "rejection_reason": null,
    "spec": {
      "version": 2,
      "spec_hash": "sha256:9c1e4b7a2d3f5e60",
      "summary": "В настройках чата есть переключатель «Тёмная тема». Выбор сохраняется в localStorage и применяется при следующем открытии.",
      "acceptance_criteria": [
        "Переключатель управляется с клавиатуры и сообщает состояние экранному диктору.",
        "Выбранная тема сохраняется после перезагрузки страницы.",
        "Без сохранённого выбора используется системная тема."
      ],
      "sources": []
    },
    "branch": "office/tsk_102",
    "base_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/12",
    "pr_number": 12,
    "head_sha": "b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80",
    "failure_kind": null,
    "checks": {
      "head_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6",
      "checks": [
        {"name": "chat-api", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000011"},
        {"name": "chat-web", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000012"},
        {"name": "chat-containers", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000013"},
        {"name": "office-api", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000014"},
        {"name": "office-web", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000015"},
        {"name": "office-sandbox", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342210771/job/52010000016"}
      ]
    },
    "pr_review": {
      "id": "prv_5d6e7f8091a2",
      "head_sha": "b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80",
      "verdict": "approve",
      "reasons": "Изменение затрагивает только настройки интерфейса и выполняет все критерии приёмки.",
      "required_changes": [],
      "checklist": [
        {"item": "paths", "ok": true, "note": "Только apps/chat/web/src/components/settings."},
        {"item": "network", "ok": true, "note": "Новых сетевых вызовов нет."},
        {"item": "external_navigation", "ok": true, "note": "Внешних ссылок нет."},
        {"item": "inputs", "ok": true, "note": "Добавлен переключатель темы, данные не отправляются."},
        {"item": "system_messages", "ok": true, "note": "Системные сообщения не скрываются."},
        {"item": "sent_text", "ok": true, "note": "Отправляемый текст не меняется."},
        {"item": "third_party_code", "ok": true, "note": "Стороннего кода нет."}
      ],
      "created_at": "2026-10-23T10:44:00Z"
    },
    "merge_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6",
    "merge_pending": false,
    "integration_started": true,
    "changed_files": [
      "web/src/components/settings/ThemeSwitch.tsx",
      "web/src/components/settings/SettingsDialog.tsx"
    ],
    "fix_cycles": 1,
    "usage": {
      "model_calls": 21,
      "model_calls_limit": 30,
      "input_tokens": 132400,
      "output_tokens": 24310
    }
  }
]
```

Ошибки: `401 unauthorized`.

### `POST /api/tasks`

Ставит задачу. Фильтр приёма выполняет детерминированные проверки, модерацию и классификацию. Ответ `201` содержит задачу в статусе `queued` или `rejected`. При недоступности любой проверки задача получает `rejected` с причиной «Проверка недоступна» (fail-closed).

Запрос:

<!-- example: TaskCreate -->
```json
{
  "text": "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена с подтверждением «Скопировано».",
  "profile": "hybrid"
}
```

Ответ `201`: модель `Task` в статусе `queued`, поля `spec`, `branch`, `base_sha`, `pr_url`, `pr_number`, `head_sha`, `failure_kind`, `checks`, `pr_review`, `merge_sha` равны `null`, `merge_pending` и `integration_started` равны `false`, `changed_files` пуст. Пример отклонённой задачи — первая задача в ответе `GET /api/tasks`.

Ошибки: `401 unauthorized`; `422 validation_error`, если `text` короче 20 или длиннее 4000 символов; `429 rate_limited`, если у участника уже пять задач за последний час.

<!-- example: ApiError -->
```json
{
  "code": "validation_error",
  "message": "Текст задачи должен содержать от 20 до 4000 символов.",
  "retryable": false,
  "operation_id": "op_0c9d8e7f"
}
```

### `GET /api/tasks/{id}`

Возвращает одну задачу (модель `Task`, пример — элементы ответа `GET /api/tasks`).

Ошибки: `401 unauthorized`; `404 not_found`.

<!-- example: ApiError -->
```json
{
  "code": "not_found",
  "message": "Задача не найдена.",
  "retryable": false,
  "operation_id": "op_4d5e6f70"
}
```

### `GET /api/tasks/{id}/events` и `GET /api/events`

Потоки SSE (`text/event-stream`). `GET /api/tasks/{id}/events` передаёт события одной задачи начиная с `seq = 1`. `GET /api/events` передаёт новые события всех задач. Каждое сообщение имеет `event: task_event`, `id: <event_id>` и `data` с моделью `TaskEvent`. Раз в 15 секунд сервер отправляет комментарий `: ping`. Перед каждым `: ping` сервер проверяет сеанс потока; если сеанс удалён или истёк, сервер завершает поток вместо отправки `: ping`. При переподключении клиент передаёт заголовок `Last-Event-ID`; сервер повторяет события с большим `event_id`. После разрыва панель сначала перечитывает `GET /api/tasks`, затем подключается к потоку.

```text
event: task_event
id: 211
data: {"event_id":211,"task_id":"tsk_101","seq":11,"type":"check_result", ...}
```

Журнал задачи `tsk_101` (поток `GET /api/tasks/tsk_101/events`):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 201,
    "task_id": "tsk_101",
    "seq": 1,
    "type": "task_created",
    "timestamp": "2026-10-23T09:00:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"title": "Кнопка копирования ответа ассистента", "profile": "hybrid"}
  },
  {
    "event_id": 202,
    "task_id": "tsk_101",
    "seq": 2,
    "type": "role_started",
    "timestamp": "2026-10-23T09:01:10Z",
    "role": "supervisor",
    "tool": null,
    "artifact": null,
    "payload": {"step": "spec"}
  },
  {
    "event_id": 203,
    "task_id": "tsk_101",
    "seq": 3,
    "type": "artifact_ready",
    "timestamp": "2026-10-23T09:03:40Z",
    "role": "supervisor",
    "tool": null,
    "artifact": "spec-v1.md",
    "payload": {"name": "spec-v1.md", "size_bytes": 1830}
  },
  {
    "event_id": 204,
    "task_id": "tsk_101",
    "seq": 4,
    "type": "role_finished",
    "timestamp": "2026-10-23T09:03:41Z",
    "role": "supervisor",
    "tool": null,
    "artifact": null,
    "payload": {"step": "spec", "outcome": "ok", "summary": "Требования и три критерия приёмки готовы."}
  },
  {
    "event_id": 205,
    "task_id": "tsk_101",
    "seq": 5,
    "type": "status_changed",
    "timestamp": "2026-10-23T09:03:42Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"from": "queued", "to": "awaiting_spec_approval"}
  },
  {
    "event_id": 206,
    "task_id": "tsk_101",
    "seq": 6,
    "type": "decision_required",
    "timestamp": "2026-10-23T09:03:42Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"decision": "approve_spec", "expected_version": 3}
  },
  {
    "event_id": 207,
    "task_id": "tsk_101",
    "seq": 7,
    "type": "decision_recorded",
    "timestamp": "2026-10-23T09:20:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"decision": "approve_spec", "decision_id": "dec_501", "participant_id": "prt_maria"}
  },
  {
    "event_id": 208,
    "task_id": "tsk_101",
    "seq": 8,
    "type": "status_changed",
    "timestamp": "2026-10-23T09:20:01Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"from": "awaiting_spec_approval", "to": "developing"}
  },
  {
    "event_id": 209,
    "task_id": "tsk_101",
    "seq": 9,
    "type": "role_started",
    "timestamp": "2026-10-23T09:20:05Z",
    "role": "developer",
    "tool": null,
    "artifact": null,
    "payload": {"step": "implement"}
  },
  {
    "event_id": 210,
    "task_id": "tsk_101",
    "seq": 10,
    "type": "tool_called",
    "timestamp": "2026-10-23T10:12:30Z",
    "role": "developer",
    "tool": "write_file",
    "artifact": null,
    "payload": {"arguments": "web/src/components/chat/MessageActions.tsx", "result": "ok"}
  },
  {
    "event_id": 211,
    "task_id": "tsk_101",
    "seq": 11,
    "type": "check_result",
    "timestamp": "2026-10-23T10:40:00Z",
    "role": "developer",
    "tool": "run_check",
    "artifact": "build.log",
    "payload": {"command": "npm run build", "exit_code": 0, "duration_ms": 18420}
  }
]
```

Фрагмент журнала задачи `tsk_102` (PR открыт, обязательные проверки PR пройдены):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 215,
    "task_id": "tsk_102",
    "seq": 35,
    "type": "pr_opened",
    "timestamp": "2026-10-23T10:20:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"pr_number": 12, "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/12"}
  },
  {
    "event_id": 216,
    "task_id": "tsk_102",
    "seq": 36,
    "type": "checks_status",
    "timestamp": "2026-10-23T10:31:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {
      "head_sha": "b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80",
      "checks": [
        {"name": "office-web", "status": "completed", "conclusion": "success", "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342200001/job/52010000005"},
        {"name": "office-paths", "status": "in_progress", "conclusion": null, "url": "https://github.com/SlavaPush/vibe-studio/actions/runs/18342200002/job/52010000007"}
      ]
    }
  }
]
```

Отменённая задача `tsk_104`: офис закрыл её PR.

<!-- example: TaskEvent -->
```json
{
  "event_id": 240,
  "task_id": "tsk_104",
  "seq": 30,
  "type": "pr_closed",
  "timestamp": "2026-10-23T12:00:00Z",
  "role": null,
  "tool": null,
  "artifact": null,
  "payload": {"pr_number": 14}
}
```

Отменённая задача `tsk_105`: исход закрытия PR не установлен за предел сверки, офис больше не повторяет операцию.

<!-- example: TaskEvent -->
```json
{
  "event_id": 250,
  "task_id": "tsk_105",
  "seq": 28,
  "type": "operation_unknown",
  "timestamp": "2026-10-23T12:40:00Z",
  "role": null,
  "tool": null,
  "artifact": null,
  "payload": {"kind": "close_pr", "pr_number": 15, "reason": "Исход операции не установлен: GitHub недоступен."}
}
```

Фрагмент журнала задачи `tsk_102` (агент ревью принял PR, офис слил его, кандидат выпуска ждёт администратора):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 217,
    "task_id": "tsk_102",
    "seq": 38,
    "type": "pr_review",
    "timestamp": "2026-10-23T10:44:00Z",
    "role": "pr_reviewer",
    "tool": null,
    "artifact": null,
    "payload": {"review_id": "prv_5d6e7f8091a2", "head_sha": "b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80", "verdict": "approve"}
  },
  {
    "event_id": 218,
    "task_id": "tsk_102",
    "seq": 39,
    "type": "merge_started",
    "timestamp": "2026-10-23T10:44:30Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"pr_number": 12, "head_sha": "b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80"}
  },
  {
    "event_id": 219,
    "task_id": "tsk_102",
    "seq": 40,
    "type": "pr_merged",
    "timestamp": "2026-10-23T10:45:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"pr_number": 12, "merge_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6"}
  },
  {
    "event_id": 220,
    "task_id": "tsk_102",
    "seq": 41,
    "type": "release_candidate",
    "timestamp": "2026-10-23T10:49:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"release_id": "rel_7", "commit_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6"}
  },
  {
    "event_id": 221,
    "task_id": "tsk_102",
    "seq": 42,
    "type": "decision_required",
    "timestamp": "2026-10-23T10:50:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"decision": "approve_release", "release_id": "rel_7", "expected_version": 1}
  }
]
```

Журнал задачи `tsk_103` (отказ фильтра приёма):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 230,
    "task_id": "tsk_103",
    "seq": 1,
    "type": "task_created",
    "timestamp": "2026-10-23T11:00:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"title": "Перевести backend vibe-chat на другую базу данных", "profile": "cloud"}
  },
  {
    "event_id": 231,
    "task_id": "tsk_103",
    "seq": 2,
    "type": "task_rejected",
    "timestamp": "2026-10-23T11:00:05Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"reason": "Задача меняет backend и схему БД. Офис принимает только изменения интерфейса vibe-chat."}
  }
]
```

Событие деплоя предыдущего выпуска:

<!-- example: TaskEvent -->
```json
{
  "event_id": 90,
  "task_id": "tsk_090",
  "seq": 37,
  "type": "deploy_status",
  "timestamp": "2026-10-22T18:12:00Z",
  "role": null,
  "tool": null,
  "artifact": null,
  "payload": {"release_id": "rel_6", "render_deploy_id": "dep-d1a2b3c4e5f6", "render_status": "live"}
}
```

Ошибки: `401 unauthorized`; `404 not_found` для неизвестной задачи. Ошибка до начала потока возвращается как JSON `ApiError`.

### `GET /api/tasks/{id}/diff`

Возвращает unified diff изменений задачи относительно `base_sha` как `text/plain; charset=utf-8`. Список изменённых файлов находится в поле `changed_files` задачи.

```diff
diff --git a/web/src/components/chat/MessageActions.tsx b/web/src/components/chat/MessageActions.tsx
new file mode 100644
--- /dev/null
+++ b/web/src/components/chat/MessageActions.tsx
@@ -0,0 +1,3 @@
+export function MessageActions() {
+  return null
+}
```

Ошибки: `401 unauthorized`; `404 not_found`; `413 diff_too_large`.

<!-- example: ApiError -->
```json
{
  "code": "diff_too_large",
  "message": "Diff больше 1 МиБ. Список файлов доступен в карточке задачи.",
  "retryable": false,
  "operation_id": "op_8a9b0c1d"
}
```

### `GET /api/tasks/{id}/checks`

Возвращает последний отчёт проверок задачи. Отчёт строится из фактических запусков команд, а не из ответа агента.

Ответ `200`:

<!-- example: CheckReport -->
```json
{
  "task_id": "tsk_101",
  "commit_sha": "e7f8a9b0c1d2e3f405162738495a6b7c8d9e0f1a",
  "spec_hash": "sha256:4b1d9e07c2a85f36",
  "suite_hash": "sha256:71c0d2e93a4b5f18",
  "checks": [
    {
      "command": "npm run lint",
      "exit_code": 0,
      "duration_ms": 2310,
      "artifact_urls": ["/api/tasks/tsk_101/artifacts/lint.log"]
    },
    {
      "command": "npm run build",
      "exit_code": 0,
      "duration_ms": 18420,
      "artifact_urls": ["/api/tasks/tsk_101/artifacts/build.log"]
    },
    {
      "command": "npx playwright test e2e/chat.spec.ts",
      "exit_code": 1,
      "duration_ms": 41200,
      "artifact_urls": [
        "/api/tasks/tsk_101/artifacts/e2e.log",
        "/api/tasks/tsk_101/artifacts/e2e-trace.zip"
      ]
    }
  ]
}
```

Если проверки ещё не запускались, `checks` пуст, `commit_sha` равен `base_sha` задачи (`null`, пока worktree не создан), `spec_hash` и `suite_hash` равны `null`. Сервер не подставляет фиктивные значения.

<!-- example: CheckReport -->
```json
{
  "task_id": "tsk_104",
  "commit_sha": null,
  "spec_hash": null,
  "suite_hash": null,
  "checks": []
}
```

Ошибки: `401 unauthorized`; `404 not_found`.

### `GET /api/tasks/{id}/artifacts/{name}`

Возвращает файл артефакта задачи с исходным `Content-Type` и заголовком `Content-Disposition: attachment` для архивов. Сервер нормализует имя и проверяет, что файл принадлежит задаче `{id}`. Артефакт другой задачи возвращается как `404 not_found`.

Артефакт — недоверенное содержимое. Каждый ответ артефакта содержит заголовки:

- `Content-Security-Policy: sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'` — скрипты HTML и SVG не выполняются, документ получает уникальный origin и не отправляет запросы с cookie офиса;
- `X-Content-Type-Options: nosniff`;
- `Cross-Origin-Resource-Policy: same-origin`.

HTML-отчёт Playwright с этими заголовками не работает интерактивно; трасса и скриншоты доступны как файлы.

Ошибки: `401 unauthorized`; `400 invalid_artifact_name`; `404 not_found`.

<!-- example: ApiError -->
```json
{
  "code": "invalid_artifact_name",
  "message": "Недопустимое имя артефакта.",
  "retryable": false,
  "operation_id": "op_2b3c4d5e"
}
```

### `POST /api/tasks/{id}/decisions`

Записывает решение владельца задачи: `approve_spec`, `cancel` или `retry`. Сервер проверяет сеанс, владельца, статус задачи и `expected_version`.

Запрос:

<!-- example: TaskDecisionRequest -->
```json
{
  "decision": "approve_spec",
  "expected_version": 3,
  "idempotency_key": "b6f1c2d3-approve-spec"
}
```

Ответ `200`:

<!-- example: Decision -->
```json
{
  "decision_id": "dec_501",
  "decision": "approve_spec",
  "task_id": "tsk_101",
  "release_id": null,
  "participant_id": "prt_maria",
  "expected_version": 3,
  "idempotency_key": "b6f1c2d3-approve-spec",
  "recorded_at": "2026-10-23T09:20:00Z"
}
```

Ошибка `403`: решение по чужой задаче.

<!-- example: ApiError -->
```json
{
  "code": "forbidden_not_owner",
  "message": "Решения по задаче принимает только её владелец.",
  "retryable": false,
  "operation_id": "op_6f7a8b9c"
}
```

Ошибка `409`: задача изменилась после того, как участник её открыл.

<!-- example: ApiError -->
```json
{
  "code": "version_mismatch",
  "message": "Задача изменилась. Обновите карточку и примите решение заново.",
  "retryable": false,
  "operation_id": "op_0d1e2f3a"
}
```

Ошибка `409`: решение недопустимо в текущем статусе.

<!-- example: ApiError -->
```json
{
  "code": "decision_not_allowed",
  "message": "Повтор доступен только для задач в статусах «Ошибка» и «Требует внимания».",
  "retryable": false,
  "operation_id": "op_4b5c6d7e"
}
```

Остальные ошибки: `401 unauthorized`; `404 not_found`; `409 idempotency_conflict`; `422 validation_error`.

### `GET /api/releases`

Возвращает очередь и историю выпусков: всех кандидатов, новые первыми. Очередь — кандидаты в статусах `pending`, `approved`, `deploying`. История — `released`, `failed`, `rolled_back`, `revoked`.

Ответ `200`:

<!-- example: ReleaseCandidate[] -->
```json
[
  {
    "id": "rel_7",
    "task_id": "tsk_102",
    "kind": "release",
    "status": "pending",
    "version": 1,
    "commit_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6",
    "expected_previous_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "spec_hash": "sha256:9c1e4b7a2d3f5e60",
    "suite_hash": "sha256:71c0d2e93a4b5f18",
    "ci_run_ids": ["18342210771"],
    "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/12",
    "created_at": "2026-10-23T10:49:00Z",
    "approved_by": null,
    "approved_at": null,
    "is_published": false,
    "deploy": null,
    "pr_review_id": "prv_5d6e7f8091a2",
    "e2e": {
      "task_id": "tsk_102",
      "commit_sha": "c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6",
      "spec_hash": "sha256:9c1e4b7a2d3f5e60",
      "suite_hash": "sha256:71c0d2e93a4b5f18",
      "checks": [
        {"command": "npm run lint", "exit_code": 0, "duration_ms": 8200, "artifact_urls": ["/api/tasks/tsk_102/artifacts/lint-3.log"]},
        {"command": "npm run test", "exit_code": 0, "duration_ms": 15400, "artifact_urls": ["/api/tasks/tsk_102/artifacts/test-3.log"]},
        {"command": "npm run build", "exit_code": 0, "duration_ms": 21900, "artifact_urls": ["/api/tasks/tsk_102/artifacts/build-3.log"]},
        {"command": "playwright test --config /office-e2e/playwright.config.ts", "exit_code": 0, "duration_ms": 47300, "artifact_urls": ["/api/tasks/tsk_102/artifacts/e2e-3.log"]}
      ]
    }
  },
  {
    "id": "rel_6",
    "task_id": "tsk_090",
    "kind": "release",
    "status": "released",
    "version": 4,
    "commit_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "expected_previous_sha": "5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90",
    "spec_hash": "sha256:2a7f0c3e9b1d4865",
    "suite_hash": "sha256:71c0d2e93a4b5f18",
    "ci_run_ids": ["18339906512"],
    "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/9",
    "created_at": "2026-10-22T17:40:00Z",
    "approved_by": "prt_admin",
    "approved_at": "2026-10-22T18:02:00Z",
    "is_published": true,
    "deploy": {
      "render_deploy_id": "dep-d1a2b3c4e5f6",
      "render_status": "live",
      "started_at": "2026-10-22T18:03:00Z",
      "finished_at": "2026-10-22T18:12:00Z",
      "published_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      "error": null
    },
    "pr_review_id": null,
    "e2e": null
  }
]
```

Ошибки: `401 unauthorized`.

### `POST /api/releases/{id}/decisions`

Записывает решение администратора: `approve_release` для кандидата в статусе `pending` или `rollback` для опубликованного выпуска. `rollback` создаёт кандидата вида `rollback` на версию `expected_previous_sha` выбранного выпуска и ставит его в ту же очередь. Подтверждение относится к точной версии кандидата: изменение `commit_sha`, `spec_hash` или `suite_hash` создаёт нового кандидата.

Запрос:

<!-- example: ReleaseDecisionRequest -->
```json
{
  "decision": "approve_release",
  "expected_version": 1,
  "idempotency_key": "4e5f6a7b-approve-rel7"
}
```

Ответ `200`:

<!-- example: Decision -->
```json
{
  "decision_id": "dec_612",
  "decision": "approve_release",
  "task_id": null,
  "release_id": "rel_7",
  "participant_id": "prt_admin",
  "expected_version": 1,
  "idempotency_key": "4e5f6a7b-approve-rel7",
  "recorded_at": "2026-10-23T11:05:00Z"
}
```

Ошибка `403`: подтверждение выпуска участником без роли `admin`.

<!-- example: ApiError -->
```json
{
  "code": "forbidden_role",
  "message": "Подтверждение выпуска и откат доступны только администратору.",
  "retryable": false,
  "operation_id": "op_8c9d0e1f"
}
```

Ошибка `409`: кандидат изменился или отозван.

<!-- example: ApiError -->
```json
{
  "code": "version_mismatch",
  "message": "Кандидат выпуска изменился. Проверьте новую версию и подтвердите заново.",
  "retryable": false,
  "operation_id": "op_3e4f5a6b"
}
```

Остальные ошибки: `401 unauthorized`; `404 not_found`; `409 decision_not_allowed`; `409 idempotency_conflict`; `422 validation_error`.

### `GET /api/slots`

Возвращает занятые и все слоты задач.

Ответ `200`:

<!-- example: Slots -->
```json
{
  "used": 2,
  "total": 3
}
```

Ошибки: `401 unauthorized`.
