# Контракт API панели офиса

Документ описывает HTTP API между панелью `web/` и API офиса `api/`. Серверные схемы находятся в `api/app/schemas.py`, типы панели — в `web/src/api/types.ts`. Имена полей и значения перечислений в обоих местах совпадают с этим документом.

С подэтапа `P4.1` API работает с БД офиса и проверяет сеанс и права на сервере. `GET /api/tasks/{id}/diff` реализуется на подэтапе `P4.2`. Панель переходит на этот API на подэтапе `P4.4`.

## Общие правила

- Базовый путь — `/api`. Тела запросов и ответов — JSON в UTF-8, кроме diff, артефактов и потоков SSE.
- Время передаётся в ISO 8601 UTC, например `2026-10-23T10:15:00Z`.
- Идентификаторы — строки с префиксом сущности: `prt_` (участник), `tsk_` (задача), `rel_` (кандидат выпуска), `dec_` (решение). Исключение: `event_id` — целое число.
- Сеанс передаётся cookie `office_session` с атрибутами `HttpOnly`, `Secure`, `SameSite=Lax`. Сервер хранит только хеш токена сеанса. Запрос без действующего сеанса получает `401 unauthorized`; исключения — `POST /api/office-session` и `DELETE /api/office-session`.
- Вход выполняется по личному одноразовому коду участника. Это тот же код, что и для чата: реестр — таблица `access_codes` чата. Один код даёт один вход в чат и один вход в офис. Роль офиса (`office_role`) и отображаемое имя (метка кода) берутся из кода при создании сеанса. Код без роли офиса в офис не входит. Ни одно поле запроса не задаёт роль и не разрешает действие.
- Отзыв кода закрывает сеансы офиса этого кода: обычный запрос получает `401 unauthorized` не позже чем через 10 секунд, открытый поток SSE завершается не позже чем через 25 секунд. Если реестр кодов недоступен, вход и проверка сеанса отклоняются с ошибкой `503 codes_unavailable`.
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
| `AgentRole` | `supervisor`, `researcher`, `developer`, `qa`, `critic`, `pr_reviewer` |
| `TaskEventType` | `task_created`, `task_rejected`, `status_changed`, `role_started`, `role_finished`, `tool_called`, `artifact_ready`, `decision_required`, `decision_recorded`, `check_result`, `release_candidate`, `deploy_status`, `pr_opened`, `checks_status`, `pr_closed`, `operation_unknown`, `pr_review`, `merge_started`, `pr_merged`, `rollback_status`, `release_blocked`, `release_unblocked`, `external_publish`, `rework_started`, `owner_review_requested`, `owner_review` |
| `FailureKind` | `checks_failed`, `pr_rejected`, `deploy_failed`, `verify_failed`, `rolled_back`, `owner_rejected` |
| `ChangeClass` | `frontend_ui`, `backend_api` |
| `Contour` | `ui`, `server` |
| `TaskDecision` | `approve_spec`, `cancel`, `retry` |
| `ReleaseDecision` | `rollback`, `resume_releases` |
| `ReleaseKind` | `release`, `rollback` |
| `ReleaseStatus` | `pending`, `deploying`, `verifying`, `rolling_back`, `released`, `failed`, `superseded`, `revoked`, `rolled_back` |
| `ReleaseOrigin` | `task`, `auto_revert`, `resume`, `manual_rollback` |

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
  "pr_merged",
  "rollback_status",
  "release_blocked",
  "release_unblocked",
  "external_publish",
  "rework_started",
  "owner_review_requested",
  "owner_review"
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
| `awaiting_integration` | Ветка проверена. Офис отправляет её в `vibe-studio`, открывает PR от GitHub App и ждёт обязательных проверок PR; сводка проверок — в поле `checks`. После успешных проверок агент ревью PR принимает решение (поле `pr_review`); при `approve` офис сливает PR (squash) и проверяет итоговый commit `merge_sha`: обязательные проверки `push` и критический E2E. PR серверного контура (`contour = server`) офис сливает только после одобрения владельца на GitHub (`owner_review`); ожидание не ограничено временем | нет |
| `awaiting_release` | PR слит, итоговый commit прошёл проверки и критический E2E; кандидат выпуска ждёт очереди выпуска. Подтверждения администратора нет | нет |
| `deploying` | Выполняется деплой кандидата, проверка после деплоя или автоматический откат | нет |
| `released` | Выпуск опубликован и прошёл проверку после деплоя; или изменение опубликовано в составе более нового выпуска | нет |
| `failed` | Задача завершилась ошибкой; результат сохранён. Причина выпуска — в `failure_kind` | нет |
| `cancelled` | Владелец отменил задачу; открытый PR задачи офис закрывает | нет |
| `needs_attention` | Исчерпан бюджет исправлений, исход внешней операции неизвестен, агент ревью PR не дал действительного решения (невалидный ответ, ошибка провайдера, исчерпанный предел; PR остаётся открытым) или выпуск остановлен: состав не подтверждён, версия сервиса недоступна, Render занят чужим деплоем, ошибка отката. Выпуск продолжает администратор решением `resume_releases` | нет |

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
| 401 | `invalid_access_code` | false | Код неизвестен, отозван, уже использован для входа в офис или не имеет роли офиса |
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
| 503 | `codes_unavailable` | true | Реестр кодов доступа недоступен или не настроен; вход и проверка сеанса временно невозможны |

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
| `approve_spec`, `cancel` чужой задачи; `retry` чужой задачи до начала интеграции | нет, `403 forbidden_not_owner` | нет, `403 forbidden_not_owner` |
| `retry` чужой задачи в `needs_attention` с `integration_started = true` | нет, `403 forbidden_not_owner` | да |
| `rollback`, `resume_releases` | нет, `403 forbidden_role` | да |

Решения по задаче доступны в статусах:

| Решение | Допустимые статусы задачи | Дополнительное условие |
| --- | --- | --- |
| `approve_spec` | `awaiting_spec_approval` | Требования готовы: `spec` не равно `null` |
| `cancel` | `queued`, `awaiting_spec_approval`, `developing`, `reviewing`, `awaiting_integration`, `needs_attention` | PR задачи не слит (`merge_sha = null`) и слияние не выполняется (`merge_pending = false`) |
| `retry` | `failed`, `needs_attention` | Бюджет обращений не исчерпан: `usage.model_calls` меньше `usage.model_calls_limit` (на этапе интеграции — см. ниже); для `failed` поле `failure_kind` пусто (`null`) |

`retry` возвращает задачу в `queued`; задача заново проходит очередь слотов. `usage` и `fix_cycles` при этом сохраняются: пределы относятся к задаче, а не к попытке. Исключение: `retry` задачи в `needs_attention` с `integration_started = true` (есть операции выпуска — push ветки, создание, слияние или закрытие PR, обращение к агенту ревью — или заполнены `head_sha`, `pr_number` или `merge_sha`) возвращает её в `awaiting_integration`. Офис сначала сверяет незавершённые операции с GitHub и продолжает с места остановки; если исход по-прежнему не установлен, задача снова получает `needs_attention`. Для такого `retry` бюджет графа (`usage.model_calls_limit`) не проверяется: чтение проверок, слияние, сверка и E2E не обращаются к моделям. Если для текущего `head_sha` нет решения агента ревью и задача ещё не слита, нужен остаток предела обращений агента ревью (`OFFICE_PR_REVIEW_CALLS` на задачу, по умолчанию 2); исчерпанный предел — `409 decision_not_allowed`. Если для итогового commit задачи уже создан кандидат выпуска (не `revoked`), `retry` недоступен (`409 decision_not_allowed`): выпуск продолжает администратор решением `resume_releases`.

Задача с `failure_kind` `checks_failed` (обязательные проверки PR или итогового commit не пройдены), `pr_rejected` (PR отклонён агентом ревью), `deploy_failed` (деплой Render завершился ошибкой), `verify_failed` (проверка после деплоя не пройдена, выполнен откат) или `rolled_back` (выпуск откатан администратором) решением `retry` не повторяется. Задачи, чьё слияние отменено revert-PR офиса (`verify_failed`, `rolled_back`, `checks_failed` итогового commit), офис сам возвращает на доработку после публикации revert — раздел «Доработка после отката» в `integrations.md`; при исчерпанном лимите доработки нужна новая задача.

Доработка: задача переходит `failed` → `queued` (событие `rework_started`) и занимает первый свободный слот раньше обычных задач `queued`, сразу в `developing` (`queued` → `developing`): требования не подтверждаются повторно. При этом переходе `branch`, `base_sha`, `head_sha`, `pr_number`, `pr_url`, `merge_sha`, `checks`, `failure_kind` очищаются, `changed_files` пуст, `fix_cycles` равен 0, предел обращений агента ревью PR начинается заново, `usage.model_calls_limit` увеличивается на предел класса задачи: 30 для `frontend_ui`, 60 для `backend_api` (расход `usage` сохраняется). Ветка доработки — `office/<task_id>-r<n>`. Предел времени `OFFICE_TASK_TIME_LIMIT_MINUTES` отсчитывается от перехода доработки в `developing`.

`cancel` в `awaiting_integration` переводит задачу в `cancelled`; офис закрывает открытый PR задачи и записывает событие `pr_closed`. После слияния PR (`merge_sha` заполнен) и во время слияния `cancel` недоступен (`409 decision_not_allowed`): изменение уже в `main` или может там оказаться.

Решения по выпуску доступны в статусах:

| Решение | Условие |
| --- | --- |
| `rollback` | Кандидат вида `release` в статусе `released` и `is_published = true`; создаётся кандидат вида `rollback` (`origin = manual_rollback`) на версию `expected_previous_sha` этого выпуска. `fix_sha` не указывается. Необязательный `reason` (до 500 символов) — причина отката: сохраняется в `reason` кандидата отката и откатанного выпуска, в событии `decision_recorded`, показывается автору задачи и передаётся разработчику доработки. После публикации отката задачи слияний офиса в составе выпуска получают `failed` с `failure_kind = rolled_back` |
| `resume_releases` с `fix_sha` | Кандидат — `release_blocked_by` очереди (выпуск заблокирован после его отката). Офис проверяет, что `fix_sha` входит в `main` и является потомком откатанного commit, выполняет проверки итогового commit и критический E2E и создаёт кандидата исправления (`origin = resume`). Тот же `fix_sha` после создания кандидата исправления (`revert.status = merged`) — `409 decision_not_allowed`, состояние не меняется |
| `resume_releases` без `fix_sha` | Кандидат остановлен (`blocked_reason` заполнен): активный кандидат очереди (`active_release_id`) или кандидат `pending`. Для активного кандидата офис сначала сверяет Render и сервис: идёт деплой или откат либо запрос деплоя ещё сверяется — `409 decision_not_allowed` с причиной; иначе БД согласуется с опубликованной версией и очередь освобождается. Кандидат `pending` возвращается в очередь. Кандидат `release_blocked_by` с `revert.status = needs_attention`: revert-PR повторяется с начала (новая попытка `revert.attempt`, прежний открытый revert-PR закрывается), блокировка сохраняется; есть операции кандидата в `started` или `unknown` либо revert-PR уже слит — `409 decision_not_allowed`. Для `release_blocked_by` в другом состоянии `revert` — `409 decision_not_allowed` |

Решение в другом статусе или при невыполненном дополнительном условии получает `409 decision_not_allowed`; `reason` в решении `resume_releases` — тоже `409 decision_not_allowed`. Без настроенного Render (`RENDER_API_KEY`, `RENDER_SERVICE_ID`) `resume_releases` недоступен. Подтверждения выпуска администратором нет: решение `approve_release` удалено, запрос с ним получает `422 validation_error`.

## Модели

### `Participant`

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | string | Идентификатор участника |
| `display_name` | string | Метка кода доступа участника, не пустая |
| `role` | `Role` | Роль офиса из кода доступа |
| `expires_at` | string (дата-время) | Окончание сеанса |

### `Task`

| Поле | Тип | Описание |
| --- | --- | --- |
| `id` | string | Идентификатор задачи |
| `title` | string | Заголовок: первая строка текста, до 80 символов |
| `text` | string | Текст задачи участника |
| `status` | `TaskStatus` | Текущий статус |
| `owner_id` | string | Идентификатор владельца |
| `owner_name` | string | Отображаемое имя владельца |
| `version` | integer | Версия объекта для `expected_version` |
| `created_at`, `updated_at` | string (дата-время) | Время создания и последнего изменения |
| `rejection_reason` | string или `null` | Причина отказа для статуса `rejected` |
| `spec` | `Spec` или `null` | Требования, когда Supervisor их подготовил |
| `branch` | string или `null` | Ветка `office/<task_id>` в `vibe-studio`; для доработки — `office/<task_id>-r<n>` |
| `base_sha` | string или `null` | Commit, от которого создан worktree |
| `pr_url` | string или `null` | Адрес PR в `vibe-studio` |
| `pr_number` | integer или `null` | Номер PR в `vibe-studio` |
| `head_sha` | string или `null` | Commit ветки `office/<task_id>`, отправленный в `vibe-studio` |
| `failure_kind` | `FailureKind` или `null` | Причина `failed` на этапе выпуска: `checks_failed` — обязательные проверки PR или итогового commit (в том числе критический E2E) не пройдены, `pr_rejected` — PR отклонён агентом ревью, `deploy_failed` — деплой Render завершился ошибкой или Render отклонил запрос (отката нет), `verify_failed` — проверка после деплоя не пройдена, выполнен откат (в том числе для выпуска, в составе которого было изменение задачи), `rolled_back` — выпуск с изменением задачи откатан администратором, `owner_rejected` — владелец отклонил PR серверного контура отзывом «Request changes» (PR закрыт, комментарий владельца — в `owner_review.comment` и в `role_finished`); `null` для остальных причин |
| `checks` | `PullRequestChecks` или `null` | Последняя сводка обязательных проверок: до слияния — проверки PR для `head_sha`, после слияния — проверки события `push` итогового commit `merge_sha` (поле `checks.head_sha` указывает проверенный commit); `null`, пока PR не проверялся и сразу после слияния |
| `pr_review` | `PullRequestReview` или `null` | Последнее решение агента ревью PR; `null`, пока агент не вызывался. Решение действительно только для своего `head_sha` |
| `merge_sha` | string или `null` | Итоговый commit squash-слияния PR в `main`; `null`, пока PR не слит |
| `merge_pending` | boolean | Слияние PR выполняется или его исход не установлен (операция `merge_pr` в статусе `started` или `unknown`); `cancel` недоступен |
| `integration_started` | boolean | Задача на этапе интеграции: есть операции журнала выпуска или заполнены `head_sha`, `pr_number`, `merge_sha`. `retry` такой задачи из `needs_attention` возвращает её в `awaiting_integration` без проверки бюджета графа |
| `changed_files` | string[] | Изменённые файлы относительно `base_sha` |
| `fix_cycles` | integer | Выполненные циклы исправления, 0–2 |
| `usage` | `Usage` | Расход задачи |
| `rework_count` | integer | Номер доработки после отката: 0 — первый проход, 1 — первая доработка |
| `rework_limit` | integer | Лимит доработок `OFFICE_REWORK_LIMIT`, по умолчанию 1 |
| `rework_reason` | string или `null` | Причина отката или непрошедших проверок итогового commit (с комментарием администратора при ручном откате); при исчерпанном лимите — с пометкой «Лимит доработки исчерпан» |
| `change_class` | `ChangeClass` | Класс задачи из фильтра приёма: `frontend_ui` — интерфейс, `backend_api` — сервер (API, контракт, схема БД, зависимости, выбор моделей, маршрутизация, уровни усилий, лимиты). Определяет права записи разработчика и QA. Для задач до появления поля — `frontend_ui` |
| `contour` | `Contour` или `null` | Контур по путям diff `base_sha..head`: `server`, если изменён хотя бы один файл серверной зоны, иначе `ui`; `null` до первого commit. Пересчитывается при каждом commit и перед решением о слиянии |
| `owner_review` | `OwnerReview` или `null` | Одобрение владельца для PR серверного контура; `null`, пока не запрошено |

`OwnerReview`: `state` (`requested` — сводка записана в описание PR, офис ждёт отзыв владельца; `approved` — отзыв `APPROVED` владельца для `head_sha`; `changes_requested` — отзыв `CHANGES_REQUESTED`, PR закрыт), `head_sha` (string — head PR, к которому относится состояние), `login` (string или `null` — логин владельца `OFFICE_OWNER_LOGIN`), `comment` (string или `null` — текст отзыва владельца). Учитывается последний отзыв владельца с `commit_id = head_sha`; отзывы других пользователей, `COMMENTED`, `DISMISSED` и отзывы на другой head не учитываются.

`Spec`: `version` (integer), `spec_hash` (string), `summary` (string), `acceptance_criteria` (string[]), `sources` (массив `{title, url}`), `plan` (string[] — короткий план реализации Supervisor: файлы и шаги в порядке выполнения, от 1 до 8 пунктов; передаётся разработчику; у требований, подготовленных до `P5.7`, — пустой список). Непустой `plan` входит в тело `spec_hash`; у требований без плана хеш считается как раньше.

`Usage`: `model_calls` (integer), `model_calls_limit` (integer; при создании задачи 30 для `frontend_ui` и отклонённой задачи, 60 для `backend_api`), `input_tokens` (integer), `output_tokens` (integer), `cost_usd` (number ≥ 0, округление до 4 знаков — оценка стоимости задачи в долларах США). `cost_usd` — сумма по всем ответам моделей задачи, включая фильтр приёма, резервного провайдера и ответы вне схемы или обрезанные: входные и выходные токены умножаются на цены ответившей модели из `policies/models.yaml` (`price_usd_per_mtok`, за 1 млн токенов). Кэш промптов и пороги длинного контекста не учитываются. Для модели без цены стоимость не прибавляется, сервер пишет предупреждение в журнал. У задач до `P5.7` — стоимость только вызовов после обновления.

`PullRequestReview`: `id` (string), `head_sha` (string — head PR, для которого принято решение), `verdict` (`approve` или `reject`), `reasons` (string — обоснование), `required_changes` (string[] — что изменить; при `reject` не пуст), `checklist` (массив `{item, ok, note}`: `item` — пункт чек-листа `paths`, `network`, `external_navigation`, `inputs`, `system_messages`, `sent_text`, `third_party_code`, `models`; `ok` — boolean; `note` — string), `created_at` (дата-время), `owner_summary` (string или `null` — сводка для владельца: зависимости, схема БД, промпты и проверки входа, маршрутизация и выбор моделей, квоты и авторизация, внешние API, рекомендация агента и факты офиса — изменённые манифесты и lock-файлы, изменение `schema.sql` с предупреждением об откате; для контура `ui` — `null`).

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
| `task_created` | `title`, `change_class` (в событиях до появления поля отсутствует); в событиях до `P5.7` также `profile`; если классификатор фильтра приёма ответил — также `provider`, `model`, `effort` модели, которая дала ответ (основная или резерв) |
| `task_rejected` | `reason` |
| `status_changed` | `from`, `to` — статусы задачи |
| `role_started` | `step` — шаг роли |
| `role_finished` | `step`, `outcome` (`ok`, `blocked`, `error`), `summary`; итог шага очереди выпуска (`step = release`: публикация, остановка, ошибка деплоя, откат) — также `release_id` |
| `tool_called` | `arguments` — краткое описание аргументов, `result` (`ok`, `denied`, `error`); для `tool = model` (обращение к модели) также `provider`, `model`, `effort` — провайдер, модель и уровень усилий вызова |
| `artifact_ready` | `name`, `size_bytes` |
| `decision_required` | `decision`, `expected_version`, при выпуске также `release_id` |
| `decision_recorded` | `decision`, `decision_id`, `participant_id`; для `rollback` с причиной также `reason` |
| `check_result` | `command`, `exit_code`, `duration_ms` |
| `release_candidate` | `release_id`, `commit_sha` |
| `deploy_status` | `release_id`, `render_deploy_id`, `render_status` — новый статус деплоя Render; после проверки после деплоя также `verified` (boolean) и `fault` (boolean — управляемый сбой `OFFICE_VERIFY_FAULT=1`, сервис не проверялся) |
| `pr_opened` | `pr_number`, `pr_url` — PR задачи создан или найден |
| `checks_status` | `head_sha`, `checks` — новая сводка обязательных проверок PR или итогового commit (модель `PullRequestChecks`) |
| `pr_closed` | `pr_number` — офис закрыл PR задачи |
| `operation_unknown` | `kind` (`push`, `create_pr`, `close_pr`, `merge_pr`), `pr_number`, `reason` — исход внешней операции закрытой задачи не установлен, офис её больше не повторяет |
| `pr_review` | `review_id`, `head_sha`, `verdict` (`approve`, `reject`) — решение агента ревью PR сохранено; роль `pr_reviewer` |
| `merge_started` | `pr_number`, `head_sha` — офис записал операцию слияния и отправляет запрос; с этого момента `merge_pending = true`, `cancel` недоступен |
| `pr_merged` | `pr_number`, `merge_sha` — офис слил PR задачи в `main` |
| `rollback_status` | `release_id`, `render_deploy_id`, `render_status` — статус отката Render (`requested` — запрос отправляется); при запросе и после проверки также `target_sha`, после проверки — `verified` |
| `release_blocked` | `release_id`, `blocked_by`, `reason` — выпуск `release_id` откатан или его итоговый commit не прошёл проверки; `blocked_by` — владелец блокировки (`release_blocked_by`): равен `release_id`, если блокировки не было, иначе слияния выпуска добавлены в набор отмены владельца |
| `release_unblocked` | `release_id`, `fixed_by` — опубликован кандидат `fixed_by`, содержащий revert-commit всех попыток набора отмены (или `fix_sha`), блокировка снята |
| `external_publish` | `published_sha` — опубликована версия без кандидата офиса (деплой вне офиса); пишется в задачи ожидающих кандидатов |
| `rework_started` | `reason`, `attempt` (номер доработки), `limit`, `source` (`verify_failed`, `rolled_back`, `checks_failed`), `release_id` (выпуск, слияние которого отменено), `materials` — источники материалов разработчику (`reason`, `deploy_events`, `render_logs`, `actions_log`, `source_diff`) |
| `owner_review_requested` | `pr_number`, `head_sha`, `pr_url`, `login` — агент ревью PR одобрил PR серверного контура, офис записал сводку в описание PR (один раз для head) и ждёт отзыв владельца `login` на GitHub |
| `owner_review` | `pr_number`, `head_sha`, `state` (`approved`, `changes_requested`), `login` — офис прочитал отзыв владельца для текущего head; при `approved` следует слияние, при `changes_requested` PR закрывается и задача получает `failed` с `failure_kind = owner_rejected` |

При исчерпанном лимите доработки вместо `rework_started` пишется `role_finished` с `step = rework`, `outcome = error` и `summary`, начинающимся с «Лимит доработки исчерпан».

События revert-PR (`tool_called` с `tool = git_revert` и `git_push`, `pr_opened`, `checks_status`, `pr_review`, `owner_review_requested`, `owner_review`, `merge_started`, `pr_merged`, `pr_closed`, `role_finished`) пишутся в задачу откатанного выпуска с дополнительным полем `revert_of` — идентификатором откатанного кандидата. Статус задачи при этом не меняется.

Панель перечитывает выпуски и состояние очереди после событий `release_candidate`, `deploy_status`, `rollback_status`, `release_blocked`, `release_unblocked`, `external_publish`, решений по выпуску (`decision_recorded` с `rollback` или `resume_releases`) и любого события с полем `release_id` или `revert_of`.

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
| `expected_previous_sha` | string | Основание выпуска: опубликованная версия, относительно которой проверен состав выпуска |
| `spec_hash` | string | Хеш требований |
| `suite_hash` | string | Хеш набора проверок; пустая строка у кандидата `pending`, пересозданного после смены опубликованной версии, пока проверки итогового commit не выполнены заново |
| `ci_run_ids` | string[] | Идентификаторы запусков обязательных проверок GitHub Actions для `commit_sha`; пуст до повторных проверок пересозданного кандидата |
| `pr_url` | string или `null` | PR, из которого получен commit; `null` для отката и исправления по `fix_sha` |
| `created_at` | string (дата-время) | Время создания |
| `is_published` | boolean | Версия этого кандидата опубликована сейчас |
| `deploy` | `DeployResult` или `null` | Результат деплоя; для кандидата вида `rollback` — деплой Render rollback |
| `pr_review_id` | string или `null` | Решение агента ревью PR (`PullRequestReview.id`) — основание выпуска; `null` для отката |
| `e2e` | `CheckReport` или `null` | Критический E2E итогового commit: `lint`, `test`, `build`, `e2e` со всеми тестами QA из `main` на `commit_sha`, с `suite_hash`; без шага `e2e`, если в commit исправления не осталось тестов QA; `null` для отката и до повторных проверок пересозданного кандидата |
| `origin` | `ReleaseOrigin` | Основание кандидата: `task` — задача, `auto_revert` — слитый revert-PR после отката, `resume` — `fix_sha` решения `resume_releases`, `manual_rollback` — решение `rollback` |
| `origin_decision_id` | string или `null` | Решение администратора — основание кандидата (`resume`, `manual_rollback`) |
| `superseded_by` | string или `null` | Кандидат, в составе которого опубликован или выбран для деплоя этот (`superseded`); `null` при внешней публикации |
| `blocked_reason` | string или `null` | Почему кандидат остановлен: состав не подтверждён, версия сервиса недоступна, Render занят, исход операции не установлен, причина `revoked` или отката. Остановленный кандидат ждёт `resume_releases` |
| `verify` | `VerifyResult` или `null` | Проверка после деплоя |
| `rollback` | `RollbackResult` или `null` | Автоматический откат после неуспешной проверки |
| `revert` | `RevertResult` или `null` | Исправление после отката: revert-PR или `fix_sha`; заполнено у владельца блокировки (`release_blocked_by`) |
| `reason` | string или `null` | Причина ручного отката из решения `rollback`: у кандидата вида `rollback` и у откатанного им выпуска; иначе `null` |

Кандидат вида `release` в статусе `failed` без `deploy` создаётся, когда обязательные проверки события `push` (`conclusion = failure`) или критический E2E итогового commit слияния офиса не пройдены: `commit_sha` — итоговый commit, `expected_previous_sha` — его родитель, `blocked_reason` — причина. Деплоя не было; кандидат блокирует выпуск до публикации revert.

Статусы кандидата: `pending` — ждёт очереди; `deploying` — деплой Render; `verifying` — проверка после деплоя; `rolling_back` — автоматический откат и его проверка; `released` — опубликован и проверен; `failed` — деплой не удался или выпуск откатан; `superseded` — опубликован или будет опубликован в составе более нового кандидата; `revoked` — опубликованная версия сменилась до деплоя, создан новый кандидат того же commit, который деплоится только после повторных проверок итогового commit (для отката — откат отменён); `rolled_back` — выпуск откатан решением `rollback`.

`DeployResult`: `render_deploy_id` (string), `render_status` (string, статус деплоя Render), `started_at` (дата-время), `finished_at` (дата-время или `null`), `published_sha` (string или `null` — версия из `GET /api/version` после успешной проверки), `error` (string или `null`).

`VerifyResult`: `expected_sha` (string), `ok` (boolean), `fault` (boolean — управляемый сбой `OFFICE_VERIFY_FAULT=1`), `attempts` (integer), `health` (boolean — `GET /healthz` вернул `{"status": "ok"}`), `version` (string или `null` — `GET /api/version`), `page_version` (string или `null` — `<meta name="app-version">` главной страницы), `error` (string или `null` — что не совпало), `checked_at` (дата-время).

`RollbackResult`: `target_sha` (string — прежняя версия), `target_deploy_id` (string — деплой Render, к которому выполняется откат), `render_deploy_id` (string или `null` — деплой отката), `render_status` (string или `null`), `started_at`, `finished_at` (дата-время или `null`), `verify` (`VerifyResult` или `null` — проверка после отката), `error` (string или `null`).

`RevertResult`: `status` (`pending` — ждёт начала, `active` — revert-PR или проверки `fix_sha` выполняются, `needs_attention` — остановлено, `merged` — кандидат исправления создан), `source` (`auto` — revert-PR офиса, `resume` — `fix_sha` администратора), `base_sha` (string или `null` — `main`, от которого создана ветка `office/revert-<release_id>`), `head_sha` (string или `null`), `pr_number`, `pr_url` (revert-PR или `null`), `checks` (`PullRequestChecks` или `null`), `fix_sha` (string или `null` — итоговый commit revert-PR или `fix_sha`), `decision_id` (string или `null`), `candidate_id` (string или `null` — кандидат исправления), `reason` (string или `null` — причина остановки), `attempt` (integer ≥ 1 — номер попытки revert-PR; растёт при повторе решением `resume_releases` без `fix_sha`, сохраняется при `resume_releases` с `fix_sha`; в записях до появления поля — 1), `tree` (string или `null` — имя worktree и ветки `office/<tree>` попытки; `null` — `revert-<release_id>`), `targets` (массив `RevertTarget` — набор отмены; пуст в записях до появления поля, тогда набор — сам кандидат), `contour` (`Contour` или `null` — контур diff revert-PR; `server` — слияние только после одобрения владельца), `owner_review` (`OwnerReview` или `null` — одобрение владельца для revert-PR серверного контура).

Revert изменения со схемой БД: после `git revert` офис возвращает `apps/chat/api/app/schema.sql` к состоянию текущего `main` (добавленные таблицы и колонки остаются в схеме и в общей БД). Если после этого ветка не отличается от `main`, revert-PR не создаётся, блокировка сохраняется, `revert.status = needs_attention` с причиной «изменение затрагивает только схему БД; проверьте текущий main и снимите блокировку решением resume_releases с fix_sha».

`RevertTarget`: `release_id` (string — выпуск, слияния которого отменяются), `source` (`verify_failed` — автоматический откат, `rolled_back` — ручной откат, `checks_failed` — непрошедшие проверки итогового commit), `base_sha`, `head_sha` (string — диапазон первой линии `main`), `merges` (string[] или `null` — найденные слияния PR офиса в диапазоне; `null`, пока revert не создан), `attempt` (integer или `null` — попытка `revert.attempt`, которая включила цель), `fixed_by` (string или `null` — итоговый commit слитого revert-PR этой попытки или принятый `fix_sha` администратора), `resumed` (boolean — цель исправлена `fix_sha` администратора: доработки по ней нет).

Набор отмены один на блокировку. Новый откат или непрошедшие проверки при установленной блокировке добавляют цель в `targets` владельца, блокировка не заменяется. Если revert-PR владельца ещё не слит, офис закрывает его и начинает новую попытку со всеми несделанными целями (один PR, revert от новых слияний к старым); если уже слит (в том числе пока проверяется его итоговый commit или проверки не прошли) — следующая попытка от нового `main` только для добавленных целей. После принятого `fix_sha` администратора (`revert.status = merged`, `source = resume`) добавленная цель запускает попытку revert-PR офиса (`source = auto`) только для неё. Пока в наборе есть цели без `fixed_by`, кандидаты не деплоятся. Блокировка снимается только публикацией кандидата, который содержит revert-commit всех попыток набора (`fixed_by` всех целей) или `fix_sha` решения `resume_releases`; публикация выпуска, начатого до сбоя, блокировку не снимает.

### `ReleaseQueueState`

| Поле | Тип | Описание |
| --- | --- | --- |
| `configured` | boolean | Исполнитель выпуска и Render настроены; `false` — «Выпуск не настроен», кандидаты ждут в `pending` |
| `active_release_id` | string или `null` | Кандидат, который сейчас деплоится, проверяется или откатывается (один на очередь) |
| `release_blocked_by` | string или `null` | Откатанный кандидат: до публикации исправления деплоятся только откаты и кандидаты, содержащие исправление |
| `blocked_reason` | string или `null` | Причина блокировки (`blocked_reason` кандидата `release_blocked_by`) |
| `published_sha` | string или `null` | Опубликованная версия без кандидата офиса (деплой вне офиса); основание следующего выпуска |

### `Slots`

`used` (integer) — задачи в статусах, занимающих слот; `total` (integer) — всего слотов, `3`.

## Операции

### `POST /api/office-session`

Создаёт сеанс участника по личному коду доступа. Ответ устанавливает cookie `office_session`; сеанс действует 7 дней. Сервер нормализует код так же, как чат (пробельные символы и дефисы удаляются, регистр не учитывается), и одной операцией отмечает вход в офис (`office_used_at`). Роль и `display_name` берутся из кода: `office_role` и метка. Каждый вход создаёт нового участника. Если запрос пришёл с действующим cookie, прежний сеанс удаляется.

Повторный вход тем же кодом получает `401 invalid_access_code`, как неизвестный, отозванный или не имеющий роли офиса код. Код без роли при такой попытке не расходуется. Если сервер отметил вход, но не создал сеанс, код остаётся использованным: автоматический повтор не выполняется, владелец выдаёт новый код.

Запрос:

<!-- example: OfficeSessionRequest -->
```json
{
  "access_code": "abcd-efgh-jkmn"
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

Ошибка `503`, если реестр кодов недоступен:

<!-- example: ApiError -->
```json
{
  "code": "codes_unavailable",
  "message": "Проверка кодов доступа временно недоступна. Повторите позже.",
  "retryable": true,
  "operation_id": "op_9c0d1e2f"
}
```

Ответ `503` не считается неверной попыткой для предела `429` и не расходует код.

Ошибки: `422 validation_error` при пустом коде или лишнем поле запроса, например `display_name`.

### `DELETE /api/office-session`

Завершает текущий сеанс. Сервер удаляет запись сеанса с хешем токена из cookie `office_session`. Участник, его задачи и другие сеансы того же и других участников сохраняются.

Ответ `204` без тела. Заголовок `Set-Cookie` удаляет cookie `office_session` с теми же атрибутами: `Path=/`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=0`.

Операция идемпотентна. Запрос без cookie, с неизвестным или истёкшим токеном тоже получает `204` и заголовок, удаляющий cookie.

После выхода тот же токен получает `401 unauthorized` во всех операциях, где нужен сеанс. Открытые потоки SSE этого сеанса завершаются не позже следующего комментария `: ping`. Повторный вход в офис тем же кодом невозможен: вход одноразовый, для нового входа нужен новый код.

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

Ошибки: `401 unauthorized` (пример в разделе «Ошибки»), в том числе после отзыва кода; `503 codes_unavailable`, если реестр кодов недоступен.

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
      "output_tokens": 85,
      "cost_usd": 0.0001
    },
    "rework_count": 0,
    "rework_limit": 1,
    "rework_reason": null,
    "change_class": "frontend_ui",
    "contour": null,
    "owner_review": null
  },
  {
    "id": "tsk_101",
    "title": "Кнопка копирования ответа ассистента",
    "text": "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена с подтверждением «Скопировано».",
    "status": "developing",
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
      ],
      "plan": [
        "apps/chat/web/src/components/chat/MessageActions.tsx: добавить кнопку «Копировать» с подписью для экранного диктора.",
        "apps/chat/web/src/components/chat/AssistantMessage.tsx: показывать MessageActions только у завершённого ответа.",
        "MessageActions.tsx: вызвать navigator.clipboard.writeText, на 2 секунды показать «Скопировано», при ошибке — «Не удалось скопировать»."
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
      "output_tokens": 9120,
      "cost_usd": 0.1876
    },
    "rework_count": 0,
    "rework_limit": 1,
    "rework_reason": null,
    "change_class": "frontend_ui",
    "contour": "ui",
    "owner_review": null
  },
  {
    "id": "tsk_102",
    "title": "Переключатель тёмной темы в настройках",
    "text": "Добавить в настройки чата переключатель светлой и тёмной темы с сохранением выбора в браузере.",
    "status": "awaiting_release",
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
      "sources": [],
      "plan": []
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
        {"item": "third_party_code", "ok": true, "note": "Стороннего кода нет."},
        {"item": "models", "ok": true, "note": "Выбор моделей и лимиты не меняются."}
      ],
      "created_at": "2026-10-23T10:44:00Z",
      "owner_summary": null
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
      "output_tokens": 24310,
      "cost_usd": 0.5079
    },
    "rework_count": 0,
    "rework_limit": 1,
    "rework_reason": null,
    "change_class": "frontend_ui",
    "contour": "ui",
    "owner_review": null
  }
]
```

Задача серверного контура ждёт одобрения владельца (фрагмент `GET /api/tasks/{id}`): `"status": "awaiting_integration"`, `"change_class": "backend_api"`, `"contour": "server"`, `"pr_review": {"verdict": "approve", "owner_summary": "Добавлено поле message_count в ответ GET /api/conversations; схема БД не меняется, зависимостей нет. Рекомендация: одобрить.", ...}`, `"owner_review": {"state": "requested", "head_sha": "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f", "login": null, "comment": null}`. После отзыва владельца «Request changes»: `"status": "failed"`, `"failure_kind": "owner_rejected"`, `"owner_review": {"state": "changes_requested", "head_sha": "5e6f…", "login": "SlavaPush", "comment": "Нужна пагинация"}`.

Задача на доработке после ручного отката (фрагмент `GET /api/tasks/{id}`): `"status": "developing"`, `"branch": "office/tsk_104-r1"`, `"rework_count": 1`, `"rework_limit": 1`, `"rework_reason": "Ручной откат администратора: После выпуска кнопка «Выйти» не завершает сеанс. Опубликована прежняя версия 8beae56 вместо 389e553."`, `"usage": {"model_calls": 14, "model_calls_limit": 60, ...}`.

Ошибки: `401 unauthorized`.

### `POST /api/tasks`

Ставит задачу. Фильтр приёма выполняет детерминированные проверки, модерацию и классификацию. Ответ `201` содержит задачу в статусе `queued` или `rejected`. При недоступности любой проверки задача получает `rejected` с причиной «Проверка недоступна» (fail-closed).

Запрос:

<!-- example: TaskCreate -->
```json
{
  "text": "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена с подтверждением «Скопировано»."
}
```

Модели выбираются по роли из одной таблицы `policies/models.yaml`. Поле `profile` удалено в `P5.7`: запрос с ним, как и с любым другим неизвестным полем, получает `422 validation_error`.

Ответ `201`: модель `Task` в статусе `queued`, поля `spec`, `branch`, `base_sha`, `pr_url`, `pr_number`, `head_sha`, `failure_kind`, `checks`, `pr_review`, `merge_sha` равны `null`, `merge_pending` и `integration_started` равны `false`, `changed_files` пуст. Пример отклонённой задачи — первая задача в ответе `GET /api/tasks`.

Ошибки: `401 unauthorized`; `422 validation_error`, если `text` короче 20 или длиннее 4000 символов или тело содержит другие поля; `429 rate_limited`, если у участника уже пять задач за последний час.

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

Потоки SSE (`text/event-stream`). `GET /api/tasks/{id}/events` передаёт события одной задачи начиная с `seq = 1`. `GET /api/events` передаёт новые события всех задач. Каждое сообщение имеет `event: task_event`, `id: <event_id>` и `data` с моделью `TaskEvent`. Раз в 15 секунд сервер отправляет комментарий `: ping`. Перед каждым `: ping` сервер проверяет сеанс потока; если сеанс удалён или истёк, код сеанса отозван или реестр кодов недоступен, сервер завершает поток вместо отправки `: ping`. При переподключении клиент передаёт заголовок `Last-Event-ID`; сервер повторяет события с большим `event_id`. После разрыва панель сначала перечитывает `GET /api/tasks`, затем подключается к потоку.

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
    "payload": {"title": "Кнопка копирования ответа ассистента", "change_class": "frontend_ui", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"}
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
    "type": "external_publish",
    "timestamp": "2026-10-23T10:50:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"published_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}
  }
]
```

Журнал выпуска с неуспешной проверкой, откатом, блокировкой и исправлением (задача `tsk_095`):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 180,
    "task_id": "tsk_095",
    "seq": 40,
    "type": "deploy_status",
    "timestamp": "2026-10-23T09:12:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"release_id": "rel_5", "render_deploy_id": "dep-e2f3a4b5c6d7", "render_status": "live", "verified": false, "fault": true}
  },
  {
    "event_id": 181,
    "task_id": "tsk_095",
    "seq": 41,
    "type": "rollback_status",
    "timestamp": "2026-10-23T09:15:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"release_id": "rel_5", "render_deploy_id": "dep-f3a4b5c6d7e8", "render_status": "live", "verified": true, "target_sha": "5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90"}
  },
  {
    "event_id": 182,
    "task_id": "tsk_095",
    "seq": 42,
    "type": "release_blocked",
    "timestamp": "2026-10-23T09:15:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"release_id": "rel_5", "blocked_by": "rel_5", "reason": "Проверка после деплоя d4e5f6a не пройдена: /healthz не ответил status ok. Выполнен откат к 5e0c1a7."}
  },
  {
    "event_id": 190,
    "task_id": "tsk_095",
    "seq": 50,
    "type": "release_unblocked",
    "timestamp": "2026-10-23T09:48:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"release_id": "rel_5", "fixed_by": "rel_8"}
  },
  {
    "event_id": 191,
    "task_id": "tsk_095",
    "seq": 51,
    "type": "rework_started",
    "timestamp": "2026-10-23T09:48:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"reason": "Проверка после деплоя d4e5f6a не пройдена: /healthz не ответил status ok. Выполнен откат к 5e0c1a7.", "attempt": 1, "limit": 1, "source": "verify_failed", "release_id": "rel_5", "materials": ["reason", "deploy_events", "render_logs", "actions_log", "source_diff"]}
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
    "payload": {"title": "Перевести backend vibe-chat на другую базу данных", "change_class": "frontend_ui", "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"}
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

Журнал задачи `tsk_105` серверного контура (ожидание и отзыв владельца):

<!-- example: TaskEvent[] -->
```json
[
  {
    "event_id": 300,
    "task_id": "tsk_105",
    "seq": 1,
    "type": "task_created",
    "timestamp": "2026-10-25T10:00:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"title": "Добавить в ответ списка диалогов число сообщений", "change_class": "backend_api"}
  },
  {
    "event_id": 340,
    "task_id": "tsk_105",
    "seq": 41,
    "type": "owner_review_requested",
    "timestamp": "2026-10-25T10:40:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"pr_number": 21, "head_sha": "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f", "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/21", "login": "SlavaPush"}
  },
  {
    "event_id": 352,
    "task_id": "tsk_105",
    "seq": 42,
    "type": "owner_review",
    "timestamp": "2026-10-25T11:05:00Z",
    "role": null,
    "tool": null,
    "artifact": null,
    "payload": {"pr_number": 21, "head_sha": "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f", "state": "approved", "login": "SlavaPush"}
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
  "payload": {"release_id": "rel_6", "render_deploy_id": "dep-d1a2b3c4e5f6", "render_status": "live", "verified": true, "fault": false}
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

Записывает решение владельца задачи: `approve_spec`, `cancel` или `retry`. Сервер проверяет сеанс, владельца, статус задачи и `expected_version`. `retry` задачи в `needs_attention` с `integration_started = true` может записать и администратор: ошибка инфраструктуры после начала интеграции не зависит от автора задачи, офис продолжает с того шага, где остановился.

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

Возвращает очередь и историю выпусков: всех кандидатов, новые первыми. Очередь — кандидаты в статусах `pending`, `deploying`, `verifying`, `rolling_back`. История — `released`, `failed`, `superseded`, `revoked`, `rolled_back`. Состояние очереди (активный кандидат, блокировка, внешняя публикация) — `GET /api/release-queue`.

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
    "is_published": false,
    "deploy": null,
    "pr_review_id": "prv_5d6e7f8091a2",
    "origin": "task",
    "origin_decision_id": null,
    "superseded_by": null,
    "blocked_reason": null,
    "verify": null,
    "rollback": null,
    "revert": null,
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
    "is_published": true,
    "deploy": {
      "render_deploy_id": "dep-d1a2b3c4e5f6",
      "render_status": "live",
      "started_at": "2026-10-22T18:03:00Z",
      "finished_at": "2026-10-22T18:12:00Z",
      "published_sha": "a1b2c3d",
      "error": null
    },
    "pr_review_id": null,
    "e2e": null,
    "origin": "task",
    "origin_decision_id": null,
    "superseded_by": null,
    "blocked_reason": null,
    "verify": {
      "expected_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      "ok": true,
      "fault": false,
      "attempts": 2,
      "health": true,
      "version": "a1b2c3d",
      "page_version": "a1b2c3d",
      "error": null,
      "checked_at": "2026-10-22T18:13:00Z"
    },
    "rollback": null,
    "revert": null
  }
]
```

Кандидат, откатанный после неуспешной проверки, с revert-PR:

<!-- example: ReleaseCandidate -->
```json
{
  "id": "rel_5",
  "task_id": "tsk_095",
  "kind": "release",
  "status": "failed",
  "version": 9,
  "commit_sha": "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d",
  "expected_previous_sha": "5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90",
  "spec_hash": "sha256:4b2e7c9d1a3f5e60",
  "suite_hash": "sha256:71c0d2e93a4b5f18",
  "ci_run_ids": ["18340010021"],
  "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/10",
  "created_at": "2026-10-23T09:02:00Z",
  "is_published": false,
  "deploy": {
    "render_deploy_id": "dep-e2f3a4b5c6d7",
    "render_status": "deactivated",
    "started_at": "2026-10-23T09:03:00Z",
    "finished_at": "2026-10-23T09:14:00Z",
    "published_sha": null,
    "error": null
  },
  "pr_review_id": "prv_1a2b3c4d5e6f",
  "e2e": null,
  "origin": "task",
  "origin_decision_id": null,
  "superseded_by": null,
  "blocked_reason": "Проверка после деплоя d4e5f6a не пройдена: /healthz не ответил status ok. Выполнен откат к 5e0c1a7.",
  "verify": {
    "expected_sha": "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d",
    "ok": false,
    "fault": false,
    "attempts": 24,
    "health": false,
    "version": "d4e5f6a",
    "page_version": "d4e5f6a",
    "error": "/healthz не ответил status ok",
    "checked_at": "2026-10-23T09:14:00Z"
  },
  "rollback": {
    "target_sha": "5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90",
    "target_deploy_id": "dep-a9b8c7d6e5f4",
    "render_deploy_id": "dep-f3a4b5c6d7e8",
    "render_status": "live",
    "started_at": "2026-10-23T09:14:00Z",
    "finished_at": "2026-10-23T09:15:00Z",
    "verify": {
      "expected_sha": "5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90",
      "ok": true,
      "fault": false,
      "attempts": 1,
      "health": true,
      "version": "5e0c1a7",
      "page_version": "5e0c1a7",
      "error": null,
      "checked_at": "2026-10-23T09:15:00Z"
    },
    "error": null
  },
  "revert": {
    "status": "active",
    "source": "auto",
    "base_sha": "e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4",
    "head_sha": "f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5",
    "pr_number": 11,
    "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/11",
    "checks": null,
    "fix_sha": null,
    "decision_id": null,
    "candidate_id": null,
    "reason": null,
    "attempt": 1,
    "tree": null
  }
}
```

Ошибки: `401 unauthorized`.

### `GET /api/release-queue`

Возвращает состояние очереди выпуска. Панель перечитывает его вместе с `GET /api/releases` по событиям `release_candidate`, `deploy_status`, `rollback_status`, `release_blocked`, `release_unblocked`, `external_publish`.

Ответ `200`:

<!-- example: ReleaseQueueState -->
```json
{
  "configured": true,
  "active_release_id": null,
  "release_blocked_by": "rel_5",
  "blocked_reason": "Проверка после деплоя d4e5f6a не пройдена: /healthz не ответил status ok. Выполнен откат к 5e0c1a7.",
  "published_sha": null
}
```

Ошибки: `401 unauthorized`.

### `POST /api/releases/{id}/decisions`

Записывает решение администратора. `rollback` для опубликованного выпуска создаёт кандидата вида `rollback` на версию `expected_previous_sha` выбранного выпуска (необязательная причина — `reason`); он ждёт свободной очереди с приоритетом перед обычными кандидатами. `resume_releases` с `fix_sha` (40 символов hex) запускает выпуск исправления после отката; без `fix_sha` — сверку остановленного выпуска или повтор revert-PR, требующего внимания, для заблокированного выпуска (раздел «Права»). Подтверждения выпуска нет: обычный кандидат деплоится автоматически.

Запрос:

<!-- example: ReleaseDecisionRequest -->
```json
{
  "decision": "resume_releases",
  "expected_version": 9,
  "idempotency_key": "4e5f6a7b-resume-rel5",
  "fix_sha": "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
}
```

Ручной откат с причиной (`reason` — до 500 символов, только для `rollback`):

<!-- example: ReleaseDecisionRequest -->
```json
{
  "decision": "rollback",
  "expected_version": 4,
  "idempotency_key": "9a8b7c6d-rollback-rel3",
  "reason": "После выпуска кнопка «Выйти» не завершает сеанс."
}
```

Ответ `200`:

<!-- example: Decision -->
```json
{
  "decision_id": "dec_612",
  "decision": "resume_releases",
  "task_id": null,
  "release_id": "rel_5",
  "participant_id": "prt_admin",
  "expected_version": 9,
  "idempotency_key": "4e5f6a7b-resume-rel5",
  "recorded_at": "2026-10-23T11:05:00Z"
}
```

Ошибка `403`: решение по выпуску участником без роли `admin`.

<!-- example: ApiError -->
```json
{
  "code": "forbidden_role",
  "message": "Откат и возобновление выпуска доступны только администратору.",
  "retryable": false,
  "operation_id": "op_8c9d0e1f"
}
```

Ошибка `409`: сверка невозможна, пока Render выполняет деплой.

<!-- example: ApiError -->
```json
{
  "code": "decision_not_allowed",
  "message": "Деплой Render dep-0a1b2c3d4e5f ещё выполняется (build_in_progress): сверка невозможна.",
  "retryable": false,
  "operation_id": "op_5a6b7c8d"
}
```

Ошибка `409`: кандидат изменился или отозван.

<!-- example: ApiError -->
```json
{
  "code": "version_mismatch",
  "message": "Кандидат выпуска изменился. Проверьте новую версию и примите решение заново.",
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
