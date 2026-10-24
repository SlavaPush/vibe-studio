import type {
  Participant,
  ReleaseCandidate,
  ReleaseDecision,
  Task,
  TaskDecision,
  TaskStatus,
} from '@/api/types'
import { FAILURE_KIND_LABELS, STATUS_LABELS } from '@/lib/labels'

export type Permission = { allowed: true } | { allowed: false; reason: string }

export const TASK_DECISION_STATUSES: Record<TaskDecision, readonly TaskStatus[]> = {
  approve_spec: ['awaiting_spec_approval'],
  cancel: [
    'queued',
    'awaiting_spec_approval',
    'developing',
    'reviewing',
    'awaiting_integration',
    'needs_attention',
  ],
  retry: ['failed', 'needs_attention'],
}

const ALLOWED: Permission = { allowed: true }

export function canAct(participant: Participant, target: Task, action: TaskDecision): Permission
export function canAct(
  participant: Participant,
  target: ReleaseCandidate,
  action: ReleaseDecision,
): Permission
export function canAct(
  participant: Participant,
  target: Task | ReleaseCandidate,
  action: TaskDecision | ReleaseDecision,
): Permission {
  if ('owner_id' in target) {
    if (participant.id !== target.owner_id) {
      return { allowed: false, reason: 'Решения по задаче принимает только её владелец.' }
    }
    if (!TASK_DECISION_STATUSES[action as TaskDecision].includes(target.status)) {
      return { allowed: false, reason: `Недоступно в статусе «${STATUS_LABELS[target.status]}».` }
    }
    if (action === 'approve_spec' && target.spec === null) {
      return { allowed: false, reason: 'Требования ещё не подготовлены.' }
    }
    if (action === 'retry' && target.status === 'failed' && target.failure_kind !== null) {
      return {
        allowed: false,
        reason: `${FAILURE_KIND_LABELS[target.failure_kind]} Повтор недоступен: поставьте новую задачу.`,
      }
    }
    if (action === 'retry' && target.usage.model_calls >= target.usage.model_calls_limit) {
      return { allowed: false, reason: 'Бюджет обращений к моделям исчерпан. Повтор недоступен.' }
    }
    return ALLOWED
  }

  if (participant.role !== 'admin') {
    return {
      allowed: false,
      reason: 'Подтверждение выпуска и откат доступны только администратору.',
    }
  }
  if (action === 'approve_release' && target.status !== 'pending') {
    return { allowed: false, reason: 'Подтвердить можно только ожидающего кандидата.' }
  }
  if (action === 'rollback' && !(target.status === 'released' && target.is_published)) {
    return { allowed: false, reason: 'Откатить можно только опубликованный сейчас выпуск.' }
  }
  return ALLOWED
}
