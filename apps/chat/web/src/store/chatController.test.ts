import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError, createApiClient, type ApiClient } from '@/api/client'
import type { ApiError, Conversation, CreateRunRequest, Message, MessageList, RunEvent } from '@/api/types'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
import { createChatController } from './chatController'
import { createChatStore, initialChatState, NO_CONVERSATION } from './conversations'

class FakeEventSource {
  static CLOSED = 2
  readyState = 1
  closed = false
  listeners = new Map<string, (event: MessageEvent<string>) => void>()
  onerror: (() => void) | null = null
  url: string
  constructor(url: string) {
    this.url = url
  }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener)
  }
  close() {
    this.closed = true
  }
  emit(event: RunEvent) {
    this.listeners.get(event.type)?.({ data: JSON.stringify(event) } as MessageEvent<string>)
  }
}

vi.stubGlobal('EventSource', FakeEventSource)

const conversation: Conversation = testState().conversations[0]

function error(status: number, code: ApiError['code']) {
  return new ApiRequestError({ code, message: code, retryable: status === 0, operation_id: 'op' }, status)
}

function fakeApi(overrides: Partial<ApiClient> = {}): ApiClient {
  const session = testState().session!
  return {
    models: vi.fn().mockResolvedValue({ items: [] }),
    me: vi.fn().mockResolvedValue(session),
    createSession: vi.fn().mockResolvedValue(session),
    forgetSession: vi.fn(),
    conversations: vi.fn().mockResolvedValue({ items: [conversation] }),
    createConversation: vi.fn().mockResolvedValue(conversation),
    renameConversation: vi.fn(),
    messages: vi.fn().mockResolvedValue({ items: [], active_run_id: null, runs: [] } satisfies MessageList),
    createRun: vi.fn(),
    run: vi.fn(),
    cancelRun: vi.fn(),
    assetUrl: vi.fn(),
    ...overrides,
  }
}

function setup(api: ApiClient) {
  const store = createChatStore(initialChatState())
  const sources: FakeEventSource[] = []
  const controller = createChatController(store, api, (url) => {
    const source = new FakeEventSource(url)
    sources.push(source)
    return source as unknown as EventSource
  })
  return { store, controller, sources }
}

describe('createChatController', () => {
  it('без сеанса показывает экран входа', async () => {
    const { store, controller } = setup(fakeApi({ me: vi.fn().mockRejectedValue(error(401, 'unauthorized')) }))
    await controller.start()
    expect(store.getState().session).toBeNull()
    expect(store.getState().sessionChecked).toBe(true)
  })

  it('после загрузки подписывается на активное задание и продолжает обновлять его диалог', async () => {
    const { run, userMessage } = testRun('run-live')
    const other: Conversation = { ...conversation, id: 'other-conversation', title: 'Другой' }
    const api = fakeApi({
      conversations: vi.fn().mockResolvedValue({ items: [conversation, other] }),
      messages: vi.fn().mockImplementation(async (id: string) =>
        id === CONVERSATION_ID
          ? { items: [userMessage], active_run_id: run.id, runs: [{ ...run, status: 'running' }] }
          : { items: [], active_run_id: null, runs: [] },
      ),
    })
    const { store, controller, sources } = setup(api)
    await controller.start()
    expect(sources.map((source) => source.url)).toEqual([`/api/runs/${run.id}/events`])

    await controller.selectConversation(other.id)
    for (const event of switchedRunEvents(run.id)) sources[0].emit(event)
    const answer = store.getState().messages[CONVERSATION_ID].find((message) => message.role === 'assistant')
    expect(answer?.text).toBe('Потоки разделяют память.')
    expect(sources[0].closed).toBe(true)

    await controller.selectConversation(CONVERSATION_ID)
    expect(api.messages).toHaveBeenCalledTimes(2)
  })

  it('не отправляет новый запрос, пока предыдущий отправляется или выполняется', async () => {
    const { run } = testRun('run-busy')
    const createRun = vi.fn().mockResolvedValue(run)
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    await Promise.all([controller.send('Первый'), controller.send('Второй')])
    await controller.send('Третий')
    expect(createRun).toHaveBeenCalledTimes(1)
    expect(store.getState().sending).toBe(false)
  })

  it('не отправляет запрос, если загруженная перед отправкой история содержит активное задание', async () => {
    const { run, userMessage } = testRun('run-in-history')
    let resolveHistory: (list: MessageList) => void = () => {}
    const createRun = vi.fn()
    const api = fakeApi({
      conversations: vi.fn().mockResolvedValue({ items: [] }),
      messages: vi.fn().mockReturnValue(new Promise<MessageList>((resolve) => (resolveHistory = resolve))),
      createRun,
    })
    const { store, controller } = setup(api)
    await controller.start()
    store.setConversations([conversation])
    const selecting = controller.selectConversation(CONVERSATION_ID)
    const sending = controller.send('Новый вопрос')
    resolveHistory({ items: [userMessage], active_run_id: run.id, runs: [{ ...run, status: 'running' }] })
    await Promise.all([selecting, sending])
    expect(createRun).not.toHaveBeenCalled()
    expect(store.getState().sending).toBe(false)
  })

  it('повторяет отправку после сетевой ошибки с тем же ключом идемпотентности', async () => {
    const { run } = testRun('run-sent')
    const createRun = vi
      .fn<(id: string, body: CreateRunRequest) => Promise<typeof run>>()
      .mockRejectedValueOnce(error(0, 'service_unavailable'))
      .mockResolvedValueOnce(run)
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Расскажи о потоках')
    expect(createRun).toHaveBeenCalledTimes(2)
    expect(createRun.mock.calls[0][1].idempotency_key).toBe(createRun.mock.calls[1][1].idempotency_key)
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.text)).toEqual(['Расскажи о потоках'])
    expect(sources).toHaveLength(1)
  })

  it('сообщает о неудачной отправке при 429 и после исчерпания повтора сетевой ошибки', async () => {
    const { run } = testRun('run-ok')
    const createRun = vi
      .fn()
      .mockRejectedValueOnce(error(429, 'rate_limited'))
      .mockRejectedValueOnce(error(0, 'service_unavailable'))
      .mockRejectedValueOnce(error(0, 'service_unavailable'))
      .mockResolvedValueOnce(run)
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    expect(await controller.send('Первый')).toEqual({ sent: false, conversationId: CONVERSATION_ID })
    expect(store.getState().errors[CONVERSATION_ID]?.code).toBe('rate_limited')
    expect(await controller.send('Второй')).toEqual({ sent: false, conversationId: CONVERSATION_ID })
    expect(store.getState().errors[CONVERSATION_ID]?.code).toBe('service_unavailable')
    expect(await controller.send('Третий')).toEqual({ sent: true, conversationId: CONVERSATION_ID })
    expect(createRun).toHaveBeenCalledTimes(4)
  })

  it('показывает ошибку, если диалог для первого сообщения не создан', async () => {
    const createConversation = vi.fn().mockRejectedValue(error(0, 'service_unavailable'))
    const { store, controller } = setup(fakeApi({ conversations: vi.fn().mockResolvedValue({ items: [] }), createConversation }))
    await controller.start()
    expect(await controller.send('Первый')).toEqual({ sent: false, conversationId: null })
    expect(store.getState().errors[NO_CONVERSATION]?.code).toBe('service_unavailable')
  })

  it('повторяет отправку, если тело успешного ответа не прочитано', async () => {
    const { run } = testRun('run-body')
    const bodies: string[] = []
    const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/runs')) {
        bodies.push(String(init?.body))
        if (bodies.length === 1) {
          return { ok: true, status: 202, json: () => Promise.reject(new TypeError('aborted')) } as Response
        }
        return new Response(JSON.stringify(run), { status: 200 })
      }
      if (url === '/api/me') return new Response(JSON.stringify(testState().session), { status: 200 })
      if (url === '/api/conversations') return new Response(JSON.stringify({ items: [conversation] }), { status: 200 })
      if (url.endsWith('/messages')) {
        return new Response(JSON.stringify({ items: [], active_run_id: null, runs: [] }), { status: 200 })
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 })
    })
    const { store, controller, sources } = setup(createApiClient(fetchImpl as typeof fetch))
    await controller.start()
    await controller.send('Расскажи о потоках')
    expect(bodies).toHaveLength(2)
    expect(bodies[0]).toBe(bodies[1])
    expect(store.getState().runs[run.id]?.status).toBe('queued')
    expect(sources).toHaveLength(1)
  })

  it('ждёт загрузки истории перед отправкой и не затирает новое сообщение старым снимком', async () => {
    const { run } = testRun('run-during-load')
    const history = testRun('run-old').userMessage
    let resolveHistory: (list: MessageList) => void = () => {}
    const createRun = vi.fn().mockResolvedValue(run)
    const api = fakeApi({
      conversations: vi.fn().mockResolvedValue({ items: [] }),
      messages: vi.fn().mockReturnValue(new Promise<MessageList>((resolve) => (resolveHistory = resolve))),
      createRun,
    })
    const { store, controller } = setup(api)
    await controller.start()
    store.setConversations([conversation])
    const selecting = controller.selectConversation(CONVERSATION_ID)
    const sending = controller.send('Новый вопрос')
    await Promise.resolve()
    expect(createRun).not.toHaveBeenCalled()
    resolveHistory({ items: [history], active_run_id: null, runs: [] })
    await Promise.all([selecting, sending])
    expect(api.messages).toHaveBeenCalledTimes(1)
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.text)).toEqual([
      history.text,
      'Новый вопрос',
    ])
  })

  it('после окончательного закрытия потока восстанавливает подписку с курсором или перечитывает историю', async () => {
    vi.useFakeTimers()
    try {
      const { run, userMessage } = testRun('run-resume')
      const runRequest = vi
        .fn()
        .mockRejectedValueOnce(error(502, 'internal_error'))
        .mockResolvedValueOnce({ ...run, status: 'running' })
        .mockResolvedValueOnce({ ...run, status: 'completed' })
      const finalAnswer: Message = { ...userMessage, id: 'final', role: 'assistant', text: 'Готово' }
      const messages = vi
        .fn()
        .mockResolvedValueOnce({ items: [userMessage], active_run_id: run.id, runs: [{ ...run, status: 'running' }] })
        .mockResolvedValueOnce({ items: [userMessage, finalAnswer], active_run_id: null, runs: [{ ...run, status: 'completed' }] })
      const { store, controller, sources } = setup(fakeApi({ run: runRequest, messages }))
      await controller.start()
      sources[0].emit(switchedRunEvents(run.id)[0])
      sources[0].readyState = FakeEventSource.CLOSED
      sources[0].onerror?.()
      expect(store.getState().runs[run.id].status).toBe('running')

      await vi.advanceTimersByTimeAsync(1000)
      expect(sources).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(2000)
      expect(sources.map((source) => source.url)).toEqual([
        `/api/runs/${run.id}/events`,
        `/api/runs/${run.id}/events?after_seq=1`,
      ])

      sources[1].readyState = FakeEventSource.CLOSED
      sources[1].onerror?.()
      await vi.advanceTimersByTimeAsync(1000)
      expect(store.getState().runs[run.id].status).toBe('completed')
      expect(store.getState().messages[CONVERSATION_ID].at(-1)?.text).toBe('Готово')
      expect(sources).toHaveLength(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('после восстановления подписывается на следующее активное задание из истории', async () => {
    vi.useFakeTimers()
    try {
      const first = testRun('run-first')
      const next = testRun('run-next')
      const messages = vi
        .fn()
        .mockResolvedValueOnce({
          items: [first.userMessage],
          active_run_id: first.run.id,
          runs: [{ ...first.run, status: 'running' }],
        })
        .mockResolvedValueOnce({
          items: [first.userMessage, next.userMessage],
          active_run_id: next.run.id,
          runs: [{ ...first.run, status: 'completed' }, { ...next.run, status: 'running' }],
        })
      const runRequest = vi.fn().mockResolvedValue({ ...first.run, status: 'completed' })
      const { store, controller, sources } = setup(fakeApi({ run: runRequest, messages }))
      await controller.start()
      sources[0].readyState = FakeEventSource.CLOSED
      sources[0].onerror?.()
      await vi.advanceTimersByTimeAsync(1000)

      expect(sources.map((source) => source.url)).toEqual([
        `/api/runs/${first.run.id}/events`,
        `/api/runs/${next.run.id}/events`,
      ])
      for (const event of switchedRunEvents(next.run.id)) sources[1].emit(event)
      const texts = store
        .getState()
        .messages[CONVERSATION_ID].filter((message) => message.role === 'assistant')
        .map((message) => message.text)
      expect(texts).toEqual(['Потоки разделяют память.'])
      expect(store.getState().runs[next.run.id].status).toBe('completed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('показывает ошибку отправки и выходит на экран входа при 401', async () => {
    const createRun = vi
      .fn()
      .mockRejectedValueOnce(error(429, 'rate_limited'))
      .mockRejectedValueOnce(error(401, 'unauthorized'))
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    expect(store.getState().errors[CONVERSATION_ID]?.code).toBe('rate_limited')
    await controller.send('Второй')
    expect(store.getState().session).toBeNull()
  })
})
