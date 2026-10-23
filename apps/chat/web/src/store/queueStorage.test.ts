import { afterEach, describe, expect, it, vi } from 'vitest'
import type { QueuedMessage } from './conversations'
import { queueKey, readQueue, writeQueue } from './queueStorage'

const item: QueuedMessage = {
  id: 'q1',
  conversationId: 'c1',
  text: 'Вопрос',
  model: 'auto',
  idempotencyKey: 'key-1',
  createdAt: '2026-10-24T05:00:00Z',
}

describe('queueStorage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('записывает и читает элементы, паузу и отслеживаемое задание отдельно для каждого пользователя', () => {
    const pause = {
      reason: 'request_failed' as const,
      error: { code: 'rate_limited' as const, message: 'Подождите', retryable: true, operation_id: 'op' },
    }
    writeQueue('u1', { items: [item], pause, trackedRunId: 'run-1' })
    expect(readQueue('u1')).toEqual({ items: [item], pause, trackedRunId: 'run-1' })
    expect(readQueue('u2')).toEqual({ items: [], pause: null, trackedRunId: null })
  })

  it('удаляет запись, когда очередь пуста и нет отслеживаемого задания', () => {
    writeQueue('u1', { items: [item], pause: null, trackedRunId: null })
    writeQueue('u1', { items: [], pause: null, trackedRunId: null })
    expect(window.sessionStorage.getItem(queueKey('u1'))).toBeNull()
  })

  it('отбрасывает невалидные данные', () => {
    window.sessionStorage.setItem(queueKey('u1'), '{не json')
    expect(readQueue('u1').items).toEqual([])
    window.sessionStorage.setItem(
      queueKey('u1'),
      JSON.stringify({ items: [item, { id: 'q2', text: 5 }, null], pause: { reason: 'unknown' }, trackedRunId: 7 }),
    )
    expect(readQueue('u1')).toEqual({ items: [item], pause: null, trackedRunId: null })
  })

  it('не падает, если sessionStorage недоступен', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => writeQueue('u1', { items: [item], pause: null, trackedRunId: null })).not.toThrow()
    expect(readQueue('u1')).toEqual({ items: [], pause: null, trackedRunId: null })
  })
})
