import type { ApiError, ModelChoice } from '@/api/types'
import { QUEUE_LIMIT, type QueuedMessage, type QueuePause, type StoredQueue } from './conversations'

const PAUSE_REASONS = new Set<QueuePause['reason']>(['failed', 'cancelled', 'check_unavailable', 'request_failed'])

export function queueKey(userId: string): string {
  return `vibe-chat:queue:${userId}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isString(value: unknown): value is string {
  return typeof value === 'string' && value !== ''
}

function readItem(value: unknown): QueuedMessage | null {
  if (!isRecord(value)) return null
  const { id, conversationId, text, model, idempotencyKey, createdAt } = value
  if (![id, conversationId, text, model, idempotencyKey, createdAt].every(isString)) return null
  return {
    id: id as string,
    conversationId: conversationId as string,
    text: text as string,
    model: model as ModelChoice,
    idempotencyKey: idempotencyKey as string,
    createdAt: createdAt as string,
  }
}

function readError(value: unknown): ApiError | undefined {
  if (!isRecord(value) || !isString(value.code) || typeof value.message !== 'string') return undefined
  return {
    code: value.code as ApiError['code'],
    message: value.message,
    retryable: value.retryable === true,
    operation_id: typeof value.operation_id === 'string' ? value.operation_id : '',
  }
}

function readPause(value: unknown): QueuePause | null {
  if (!isRecord(value) || !PAUSE_REASONS.has(value.reason as QueuePause['reason'])) return null
  const error = readError(value.error)
  return error ? { reason: value.reason as QueuePause['reason'], error } : { reason: value.reason as QueuePause['reason'] }
}

export function emptyQueue(): StoredQueue {
  return { items: [], pause: null, trackedRunId: null }
}

export function readQueue(userId: string): StoredQueue {
  try {
    const raw = window.sessionStorage.getItem(queueKey(userId))
    if (!raw) return emptyQueue()
    const value: unknown = JSON.parse(raw)
    if (!isRecord(value)) return emptyQueue()
    const items = (Array.isArray(value.items) ? value.items : [])
      .map(readItem)
      .filter((item): item is QueuedMessage => item !== null)
      .slice(0, QUEUE_LIMIT)
    return {
      items,
      pause: items.length > 0 ? readPause(value.pause) : null,
      trackedRunId: isString(value.trackedRunId) ? value.trackedRunId : null,
    }
  } catch {
    return emptyQueue()
  }
}

export function writeQueue(userId: string, queue: StoredQueue): void {
  try {
    const key = queueKey(userId)
    if (queue.items.length === 0 && !queue.pause && !queue.trackedRunId) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, JSON.stringify(queue))
  } catch {
    return
  }
}
