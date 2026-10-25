import type {
  AgentRole,
  Contour,
  FailureKind,
  ModelProfile,
  ReleaseDecision,
  ReleaseOrigin,
  ReleaseStatus,
  Task,
  TaskDecision,
  TaskEventType,
  TaskStatus,
} from '@/api/types'

export const STATUS_LABELS: Record<TaskStatus, string> = {
  rejected: 'Отклонена',
  queued: 'В очереди',
  awaiting_spec_approval: 'Ждёт утверждения требований',
  developing: 'Разработка',
  reviewing: 'Проверка',
  awaiting_integration: 'Ждёт слияния PR',
  awaiting_release: 'Ждёт выпуска',
  deploying: 'Деплой',
  released: 'Выпущена',
  failed: 'Ошибка',
  cancelled: 'Отменена',
  needs_attention: 'Требует внимания',
}

export const RELEASE_STATUS_LABELS: Record<ReleaseStatus, string> = {
  pending: 'Ждёт очереди',
  deploying: 'Деплой',
  verifying: 'Проверка после деплоя',
  rolling_back: 'Откат',
  released: 'Опубликован',
  failed: 'Ошибка',
  superseded: 'В составе нового выпуска',
  revoked: 'Отозван',
  rolled_back: 'Откачен',
}

export const RELEASE_ORIGIN_LABELS: Record<ReleaseOrigin, string> = {
  task: 'Задача',
  auto_revert: 'Revert после отката',
  resume: 'Исправление владельца',
  manual_rollback: 'Ручной откат',
}

export const DECISION_LABELS: Record<TaskDecision | ReleaseDecision, string> = {
  approve_spec: 'Утвердить требования',
  cancel: 'Отменить задачу',
  retry: 'Повторить',
  rollback: 'Откатить',
  resume_releases: 'Возобновить выпуск',
}

export const AGENT_ROLE_LABELS: Record<AgentRole, string> = {
  supervisor: 'Supervisor',
  researcher: 'Исследователь',
  developer: 'Разработчик',
  qa: 'QA',
  critic: 'Критик',
  pr_reviewer: 'Агент ревью PR',
}

export const EVENT_TYPE_LABELS: Record<TaskEventType, string> = {
  task_created: 'Задача создана',
  task_rejected: 'Задача отклонена',
  status_changed: 'Смена статуса',
  role_started: 'Роль начала работу',
  role_finished: 'Роль завершила работу',
  tool_called: 'Вызов инструмента',
  artifact_ready: 'Артефакт готов',
  decision_required: 'Требуется решение',
  decision_recorded: 'Решение записано',
  check_result: 'Результат проверки',
  release_candidate: 'Кандидат выпуска',
  deploy_status: 'Статус деплоя',
  pr_opened: 'PR открыт',
  checks_status: 'Проверки PR',
  pr_closed: 'PR закрыт',
  operation_unknown: 'Исход операции неизвестен',
  pr_review: 'Решение агента ревью PR',
  merge_started: 'Слияние PR начато',
  pr_merged: 'PR слит',
  rollback_status: 'Статус отката',
  release_blocked: 'Выпуск заблокирован',
  release_unblocked: 'Блокировка снята',
  external_publish: 'Публикация вне офиса',
  rework_started: 'Доработка после отката',
  owner_review_requested: 'Ждёт одобрения владельца',
  owner_review: 'Отзыв владельца',
}

export const FAILURE_KIND_LABELS: Record<FailureKind, string> = {
  checks_failed: 'Обязательные проверки PR не пройдены, PR закрыт.',
  pr_rejected: 'Агент ревью отклонил PR.',
  deploy_failed: 'Деплой выпуска на Render не выполнен.',
  verify_failed: 'Проверка после деплоя не пройдена, выпуск откатан.',
  rolled_back: 'Выпуск откатан администратором.',
  owner_rejected: 'Владелец отклонил PR, PR закрыт.',
}

export const CONTOUR_LABELS: Record<Contour, string> = {
  ui: 'Интерфейс',
  server: 'Сервер',
}

export function failureLabel(task: Task): string | null {
  if (task.failure_kind === null) return null
  if (task.failure_kind === 'checks_failed' && task.merge_sha !== null) {
    return 'Обязательные проверки или критический E2E итогового commit не пройдены.'
  }
  return FAILURE_KIND_LABELS[task.failure_kind]
}

export const REVIEW_VERDICT_LABELS: Record<'approve' | 'reject', string> = {
  approve: 'Принят',
  reject: 'Отклонён',
}

export const CHECKLIST_LABELS: Record<string, string> = {
  paths: 'Изменённые пути',
  network: 'Новые сетевые вызовы',
  external_navigation: 'Навигация на внешние URL',
  inputs: 'Новые поля ввода',
  system_messages: 'Скрытие системных сообщений',
  sent_text: 'Изменение отправляемого текста',
  third_party_code: 'Скопированный сторонний код',
  models: 'Выбор моделей, маршрутизация, уровни усилий, лимиты',
}

export const CHECK_STATUS_LABELS: Record<string, string> = {
  missing: 'Нет запуска',
  queued: 'В очереди',
  in_progress: 'Выполняется',
  completed: 'Завершена',
}

export const PROFILE_LABELS: Record<ModelProfile, string> = {
  local: 'Локальный',
  cloud: 'Облачный',
  hybrid: 'Гибридный',
}

const dateTime = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' })

export function formatDateTime(value: string): string {
  return dateTime.format(new Date(value))
}
