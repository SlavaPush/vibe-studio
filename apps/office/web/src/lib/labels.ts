import type {
  AgentRole,
  FailureKind,
  ModelProfile,
  ReleaseDecision,
  ReleaseStatus,
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
  pending: 'Ждёт подтверждения',
  approved: 'Подтверждён',
  deploying: 'Деплой',
  released: 'Опубликован',
  failed: 'Ошибка',
  rolled_back: 'Откачен',
  revoked: 'Отозван',
}

export const DECISION_LABELS: Record<TaskDecision | ReleaseDecision, string> = {
  approve_spec: 'Утвердить требования',
  cancel: 'Отменить задачу',
  retry: 'Повторить',
  approve_release: 'Подтвердить выпуск',
  rollback: 'Откатить',
}

export const AGENT_ROLE_LABELS: Record<AgentRole, string> = {
  supervisor: 'Supervisor',
  researcher: 'Исследователь',
  developer: 'Разработчик',
  qa: 'QA',
  critic: 'Критик',
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
}

export const FAILURE_KIND_LABELS: Record<FailureKind, string> = {
  checks_failed: 'Обязательные проверки PR не пройдены, PR закрыт.',
  pr_rejected: 'Агент ревью отклонил PR.',
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
