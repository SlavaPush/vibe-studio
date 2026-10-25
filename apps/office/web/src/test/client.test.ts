import { describe, expect, it } from 'vitest'
import {
  ApiRequestError,
  NETWORK_MESSAGE,
  SERVER_MESSAGE,
  createTask,
  decideTask,
  listTasks,
  login,
  me,
  taskDiff,
} from '@/api/client'
import { apiError, fakeApi, json } from '@/test/fake-api'

async function failure(promise: Promise<unknown>): Promise<ApiRequestError> {
  try {
    await promise
  } catch (error) {
    if (error instanceof ApiRequestError) return error
    throw error
  }
  throw new Error('запрос завершился успешно')
}

describe('клиент API', () => {
  it('отправляет относительный адрес с cookie того же origin', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks', [])
    await expect(listTasks()).resolves.toEqual([])
    expect(api.fetch).toHaveBeenCalledWith(
      '/api/tasks',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
  })

  it('отправляет JSON в POST', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_1/decisions', { decision_id: 'dec_1' })
    const body = { decision: 'cancel' as const, expected_version: 3, idempotency_key: 'key-12345678' }
    await decideTask('tsk_1', body)
    const [, init] = api.fetch.mock.calls[0]
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json')
    expect(api.bodies('POST /api/tasks/tsk_1/decisions')).toEqual([body])
  })

  it('разбирает ApiError и паузу Retry-After', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks', () =>
      apiError(429, 'rate_limited', 'Можно поставить не больше пяти задач за час.', { 'Retry-After': '42' }),
    )
    const error = await failure(createTask({ text: 'x'.repeat(30) }))
    expect(error).toMatchObject({
      status: 429,
      code: 'rate_limited',
      message: 'Можно поставить не больше пяти задач за час.',
      retryable: true,
      retryAfter: 42,
    })
  })

  it('показывает ошибку входа из ответа сервера', async () => {
    const api = fakeApi()
    api.on('POST /api/office-session', () =>
      apiError(401, 'invalid_access_code', 'Неверный код доступа офиса.'),
    )
    const error = await failure(login({ access_code: 'x' }))
    expect(error).toMatchObject({ status: 401, code: 'invalid_access_code', retryAfter: null })
    expect(api.bodies('POST /api/office-session')).toEqual([{ access_code: 'x' }])
  })

  it('показывает текст сервера для 503 codes_unavailable', async () => {
    const api = fakeApi()
    api.on('GET /api/me', () =>
      json(503, { code: 'codes_unavailable', message: 'Реестр недоступен.', retryable: true, operation_id: 'op_1' }),
    )
    const error = await failure(me())
    expect(error).toMatchObject({ status: 503, code: 'codes_unavailable', retryable: true, message: 'Реестр недоступен.' })
  })

  it('заменяет текст ошибки 500 фиксированным', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks', () => json(500, { code: 'internal_error', message: 'stack trace' }))
    const error = await failure(listTasks())
    expect(error).toMatchObject({ status: 500, code: 'internal_error', message: SERVER_MESSAGE })
  })

  it('ответ без JSON становится внутренней ошибкой', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks', () => new Response('Bad Gateway', { status: 502 }))
    const error = await failure(listTasks())
    expect(error).toMatchObject({ status: 502, code: 'internal_error', message: SERVER_MESSAGE })
  })

  it('сетевая ошибка получает код network', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks', () => {
      throw new TypeError('Failed to fetch')
    })
    const error = await failure(listTasks())
    expect(error).toMatchObject({ status: 0, code: 'network', message: NETWORK_MESSAGE })
  })

  it('обрыв при чтении тела успешного ответа получает код network', async () => {
    const api = fakeApi()
    const broken = () =>
      ({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () => Promise.reject(new TypeError('network error')),
        text: () => Promise.reject(new TypeError('network error')),
      }) as unknown as Response
    api.on('POST /api/tasks/tsk_1/decisions', broken)
    api.on('GET /api/tasks', broken)
    api.on('GET /api/tasks/tsk_1/diff', broken)
    const body = { decision: 'cancel' as const, expected_version: 3, idempotency_key: 'key-12345678' }
    for (const promise of [decideTask('tsk_1', body), listTasks(), taskDiff('tsk_1')]) {
      expect(await failure(promise)).toMatchObject({ status: 0, code: 'network', message: NETWORK_MESSAGE })
    }
  })

  it('возвращает diff как текст', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks/tsk_1/diff', () => new Response('diff --git a/x b/x\n', { status: 200 }))
    await expect(taskDiff('tsk_1')).resolves.toBe('diff --git a/x b/x\n')
  })
})
