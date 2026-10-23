import { vi } from 'vitest'
import type { TaskEvent } from '@/api/types'

export interface Call {
  method: string
  path: string
  body: unknown
}

type Handler = (call: Call) => Response | Promise<Response>

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

export function apiError(
  status: number,
  code: string,
  message: string,
  headers: Record<string, string> = {},
): Response {
  return json(status, { code, message, retryable: status === 429, operation_id: 'op_test' }, headers)
}

export const unauthorized = () =>
  apiError(401, 'unauthorized', 'Сеанс не найден или истёк. Войдите по коду офиса.')

export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

export function fakeApi() {
  const routes = new Map<string, Handler>()
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: Call = {
      method: init?.method ?? 'GET',
      path: String(input),
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    }
    calls.push(call)
    const handler = routes.get(`${call.method} ${call.path}`)
    if (!handler) return apiError(404, 'not_found', 'Объект не найден.')
    return handler(call)
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    calls,
    fetch: fetchMock,
    on(route: string, response: unknown) {
      routes.set(
        route,
        typeof response === 'function' ? (response as Handler) : () => json(200, response),
      )
    },
    count(route: string) {
      return calls.filter((call) => `${call.method} ${call.path}` === route).length
    },
    bodies(route: string) {
      return calls.filter((call) => `${call.method} ${call.path}` === route).map((call) => call.body)
    },
  }
}

type Listener = (event: Event) => void

export class FakeEventSource {
  static instances: FakeEventSource[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  readonly url: string
  readyState = 0
  closed = false
  private listeners = new Map<string, Set<Listener>>()

  constructor(url: string | URL) {
    this.url = String(url)
    FakeEventSource.instances.push(this)
  }

  static all(url: string) {
    return FakeEventSource.instances.filter((source) => source.url === url)
  }

  static last(url: string) {
    const sources = FakeEventSource.all(url)
    return sources[sources.length - 1]
  }

  addEventListener(type: string, listener: Listener) {
    const set = this.listeners.get(type) ?? new Set<Listener>()
    set.add(listener)
    this.listeners.set(type, set)
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener)
  }

  close() {
    this.closed = true
    this.readyState = 2
  }

  private dispatch(type: string, event: Event) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  open() {
    this.readyState = 1
    this.dispatch('open', new Event('open'))
  }

  emit(event: TaskEvent) {
    this.dispatch(
      'task_event',
      new MessageEvent('task_event', { data: JSON.stringify(event), lastEventId: String(event.event_id) }),
    )
  }

  fail(state: 'reconnecting' | 'closed' = 'reconnecting') {
    this.readyState = state === 'closed' ? 2 : 0
    this.dispatch('error', new Event('error'))
  }
}

export function installEventSource() {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
}

let nextEventId = 1000

export function taskEvent(
  taskId: string,
  type: TaskEvent['type'],
  payload: Record<string, unknown> = {},
): TaskEvent {
  nextEventId += 1
  return {
    event_id: nextEventId,
    task_id: taskId,
    seq: nextEventId,
    type,
    timestamp: '2026-10-24T10:00:00Z',
    role: null,
    tool: null,
    artifact: null,
    payload,
  }
}
