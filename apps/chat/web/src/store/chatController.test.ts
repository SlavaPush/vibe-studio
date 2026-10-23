import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError, createApiClient, type ApiClient } from '@/api/client'
import type { ApiError, Conversation, CreateRunRequest, Message, MessageList, RunEvent } from '@/api/types'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
import { createChatController } from './chatController'
import { createChatStore, initialChatState, NO_CONVERSATION, type QueuedMessage } from './conversations'
import { queueKey, readQueue, writeQueue } from './queueStorage'

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

  it('не отправляет новый запрос, пока предыдущий отправляется или выполняется, а ставит его в очередь', async () => {
    const { run } = testRun('run-busy')
    const createRun = vi.fn().mockResolvedValue(run)
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    await Promise.all([controller.send('Первый'), controller.send('Второй')])
    await controller.send('Третий')
    expect(createRun).toHaveBeenCalledTimes(1)
    expect(store.getState().sending).toBe(false)
    expect(store.getState().queue.map((item) => item.text)).toEqual(['Второй', 'Третий'])
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

  it('показывает сообщение пользователя до ответа createRun и заменяет его серверным без дубликата', async () => {
    const { run } = testRun('run-optimistic')
    let resolveRun: (value: typeof run) => void = () => {}
    const createRun = vi.fn().mockReturnValue(new Promise((resolve) => (resolveRun = resolve)))
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    const sending = controller.send('Расскажи о потоках')
    await vi.waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
    const before = store.getState().messages[CONVERSATION_ID]
    expect(before.map((message) => message.text)).toEqual(['Расскажи о потоках'])
    expect(before[0].id).toMatch(/^pending:/)

    resolveRun(run)
    await sending
    const after = store.getState().messages[CONVERSATION_ID]
    expect(after.map((message) => message.id)).toEqual([run.user_message_id])
  })

  it('после ошибки createRun удаляет временное сообщение', async () => {
    let rejectRun: (reason: unknown) => void = () => {}
    const createRun = vi.fn().mockReturnValue(new Promise((_, reject) => (rejectRun = reject)))
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    const sending = controller.send('Расскажи о потоках')
    await vi.waitFor(() => expect(store.getState().messages[CONVERSATION_ID]).toHaveLength(1))
    rejectRun(error(500, 'internal_error'))
    expect(await sending).toEqual({ sent: false, conversationId: CONVERSATION_ID })
    expect(store.getState().messages[CONVERSATION_ID]).toEqual([])
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

type Finish = 'completed' | 'refusal' | 'check_unavailable' | 'failed' | 'cancelled'

function terminal(runId: string, kind: Finish): RunEvent {
  const base = { event_id: `${runId}-end`, run_id: runId, seq: 100, timestamp: '2026-10-23T18:01:00Z' }
  const message: Message = {
    ...testRun(runId).userMessage,
    id: `${runId}-end`,
    role: kind === 'completed' ? 'assistant' : 'system',
    kind: kind === 'refusal' || kind === 'check_unavailable' ? kind : null,
    text: 'Готово',
  }
  switch (kind) {
    case 'completed':
      return { ...base, type: 'completed', payload: { message } }
    case 'refusal':
      return { ...base, type: 'refusal', payload: { message } }
    case 'check_unavailable':
      return { ...base, type: 'check_unavailable', payload: { message } }
    default:
      return {
        ...base,
        type: 'failed',
        payload: {
          error: {
            code: kind === 'cancelled' ? 'run_cancelled' : 'provider_unavailable',
            message: 'Ошибка',
            retryable: false,
            operation_id: 'op',
          },
        },
      }
  }
}

function runFactory() {
  let count = 0
  return vi.fn(async (conversationId: string, body: CreateRunRequest) => {
    count += 1
    const { run } = testRun(`run-${count}`)
    return { ...run, conversation_id: conversationId, requested_model: body.model }
  })
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

function queueItem(text: string, conversationId = CONVERSATION_ID): QueuedMessage {
  return {
    id: `item-${text}`,
    conversationId,
    text,
    model: 'claude-sonnet-5-5',
    idempotencyKey: `key-${text}`,
    createdAt: '2026-10-24T05:00:00Z',
  }
}

function emit(sources: FakeEventSource[], runId: string, kind: Finish) {
  const source = sources.find((item) => item.url.startsWith(`/api/runs/${runId}/events`))
  if (!source) throw new Error(`нет подписки на ${runId}`)
  source.emit(terminal(runId, kind))
}

describe('очередь сообщений', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    window.localStorage.clear()
  })

  it('ставит сообщения во время ответа в очередь и отправляет их по порядку после завершения', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    expect(await controller.send('Первый')).toEqual({ sent: true, conversationId: CONVERSATION_ID })
    expect(await controller.send('Второй')).toEqual({ sent: true, conversationId: CONVERSATION_ID })
    expect(await controller.send('Третий')).toEqual({ sent: true, conversationId: CONVERSATION_ID })
    expect(createRun).toHaveBeenCalledTimes(1)
    expect(store.getState().queue.map((item) => item.text)).toEqual(['Второй', 'Третий'])

    emit(sources, 'run-1', 'completed')
    await flush()
    expect(createRun).toHaveBeenCalledTimes(2)
    expect(createRun.mock.calls[1][1].text).toBe('Второй')
    expect(store.getState().queue.map((item) => item.text)).toEqual(['Третий'])

    emit(sources, 'run-2', 'completed')
    await flush()
    expect(createRun.mock.calls.map(([, body]) => body.text)).toEqual(['Первый', 'Второй', 'Третий'])
    expect(store.getState().queue).toEqual([])
    const users = store.getState().messages[CONVERSATION_ID].filter((message) => message.role === 'user')
    expect(users.map((message) => message.id)).toEqual(['run-1-user', 'run-2-user', 'run-3-user'])
  })

  it('принимает не больше 5 сообщений', async () => {
    const createRun = runFactory()
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Ответ')
    for (const text of ['1', '2', '3', '4', '5']) {
      expect((await controller.send(text)).sent).toBe(true)
    }
    expect(await controller.send('6')).toEqual({ sent: false, conversationId: CONVERSATION_ID })
    expect(store.getState().queue).toHaveLength(5)
  })

  it('фиксирует модель и ключ идемпотентности при постановке и повторяет отправку с тем же ключом', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    store.selectModel('claude-sonnet-5-5')
    await controller.send('Второй')
    store.selectModel('image')
    const [queued] = store.getState().queue
    expect(queued.model).toBe('claude-sonnet-5-5')

    createRun.mockRejectedValueOnce(error(500, 'internal_error'))
    emit(sources, 'run-1', 'completed')
    await flush()
    expect(store.getState().queuePause?.reason).toBe('request_failed')
    expect(store.getState().queue).toEqual([queued])

    controller.resumeQueue()
    await flush()
    const bodies = createRun.mock.calls.slice(1).map(([, body]) => body)
    expect(bodies).toEqual([
      { text: 'Второй', model: 'claude-sonnet-5-5', idempotency_key: queued.idempotencyKey },
      { text: 'Второй', model: 'claude-sonnet-5-5', idempotency_key: queued.idempotencyKey },
    ])
    expect(store.getState().queue).toEqual([])
    expect(store.getState().queuePause).toBeNull()
  })

  it('удалённое сообщение не отправляется', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    await controller.send('Третий')
    controller.removeQueued(store.getState().queue[0].id)
    emit(sources, 'run-1', 'completed')
    await flush()
    expect(createRun.mock.calls.map(([, body]) => body.text)).toEqual(['Первый', 'Третий'])
  })

  it.each(['failed', 'cancelled', 'check_unavailable'] as const)(
    'при завершении задания со статусом %s очередь ставится на паузу до «Продолжить»',
    async (kind) => {
      const createRun = runFactory()
      const { store, controller, sources } = setup(fakeApi({ createRun }))
      await controller.start()
      await controller.send('Первый')
      await controller.send('Второй')
      emit(sources, 'run-1', kind)
      await flush()
      expect(store.getState().queuePause).toEqual({ reason: kind })
      expect(createRun).toHaveBeenCalledTimes(1)
      expect(store.getState().queue).toHaveLength(1)

      controller.resumeQueue()
      await flush()
      expect(createRun).toHaveBeenCalledTimes(2)
      expect(store.getState().queue).toEqual([])
    },
  )

  it('«Остановить» ставит очередь на паузу после отмены задания', async () => {
    const createRun = runFactory()
    const cancelRun = vi.fn().mockResolvedValue({})
    const { store, controller, sources } = setup(fakeApi({ createRun, cancelRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    await controller.cancel()
    expect(cancelRun).toHaveBeenCalledWith('run-1')
    emit(sources, 'run-1', 'cancelled')
    await flush()
    expect(store.getState().queuePause).toEqual({ reason: 'cancelled' })
    expect(createRun).toHaveBeenCalledTimes(1)
  })

  it.each(['completed', 'refusal'] as const)('после статуса %s очередь продолжается', async (kind) => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    emit(sources, 'run-1', kind)
    await flush()
    expect(store.getState().queuePause).toBeNull()
    expect(createRun).toHaveBeenCalledTimes(2)
  })

  it('отказ сервера rate_limited при отправке из очереди ставит паузу с текстом ошибки', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    createRun.mockRejectedValueOnce(error(429, 'rate_limited'))
    emit(sources, 'run-1', 'completed')
    await flush()
    expect(store.getState().queuePause).toMatchObject({ reason: 'request_failed', error: { code: 'rate_limited' } })
    expect(store.getState().queue.map((item) => item.text)).toEqual(['Второй'])
    expect(store.getState().messages[CONVERSATION_ID].some((message) => message.id.startsWith('pending:'))).toBe(false)
  })

  it('ошибка прямой отправки при непустой очереди ставит очередь на паузу', async () => {
    let rejectRun: (reason: unknown) => void = () => {}
    const createRun = vi.fn().mockReturnValue(new Promise((_, reject) => (rejectRun = reject)))
    const { store, controller } = setup(fakeApi({ createRun }))
    await controller.start()
    const first = controller.send('Первый')
    await vi.waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
    expect((await controller.send('Второй')).sent).toBe(true)
    rejectRun(error(500, 'internal_error'))
    expect(await first).toEqual({ sent: false, conversationId: CONVERSATION_ID })
    await flush()
    expect(store.getState().queuePause).toMatchObject({ reason: 'request_failed', error: { code: 'internal_error' } })
    expect(store.getState().queue.map((item) => item.text)).toEqual(['Второй'])
    expect(createRun).toHaveBeenCalledTimes(1)
  })

  it('во время отправки из очереди показывает временное сообщение', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    let resolveRun: (value: Awaited<ReturnType<typeof createRun>>) => void = () => {}
    createRun.mockReturnValueOnce(new Promise((resolve) => (resolveRun = resolve)))
    emit(sources, 'run-1', 'completed')
    await flush()
    const texts = () =>
      store.getState().messages[CONVERSATION_ID].filter((message) => message.role === 'user').map((message) => message.id)
    expect(texts()).toEqual(['run-1-user', `pending:${store.getState().queue[0].idempotencyKey}`])
    expect(store.getState().sendingQueuedId).toBe(store.getState().queue[0].id)

    resolveRun({ ...testRun('run-2').run })
    await flush()
    expect(texts()).toEqual(['run-1-user', 'run-2-user'])
  })

  it('сохраняет очередь в sessionStorage и очищает запись, когда очередь пуста', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    const stored = readQueue('test-user')
    expect(stored.items.map((item) => item.text)).toEqual(['Второй'])
    expect(stored.trackedRunId).toBe('run-1')

    emit(sources, 'run-1', 'completed')
    await flush()
    expect(readQueue('test-user')).toEqual({ items: [], pause: null, trackedRunId: 'run-2' })
    emit(sources, 'run-2', 'completed')
    expect(window.sessionStorage.getItem(queueKey('test-user'))).toBeNull()
    expect(store.getState().queue).toEqual([])
  })

  it.each(['failed', 'cancelled', 'check_unavailable', 'request_failed'] as const)(
    'после перезагрузки на паузе %s не отправляет до «Продолжить»',
    async (reason) => {
      writeQueue('test-user', { items: [queueItem('Сохранённый')], pause: { reason }, trackedRunId: null })
      const createRun = runFactory()
      const { store, controller } = setup(fakeApi({ createRun }))
      await controller.start()
      await flush()
      expect(store.getState().queue.map((item) => item.text)).toEqual(['Сохранённый'])
      expect(store.getState().queuePause).toEqual({ reason })
      expect(createRun).not.toHaveBeenCalled()

      controller.resumeQueue()
      await flush()
      expect(createRun).toHaveBeenCalledWith(CONVERSATION_ID, {
        text: 'Сохранённый',
        model: 'claude-sonnet-5-5',
        idempotency_key: 'key-Сохранённый',
      })
    },
  )

  it('после перезагрузки ждёт отслеживаемое задание в другом диалоге и только потом отправляет очередь', async () => {
    const other: Conversation = { ...conversation, id: 'other-conversation', title: 'Другой' }
    window.localStorage.setItem('vibe-chat:active:test-user', other.id)
    writeQueue('test-user', { items: [queueItem('Во втором', other.id)], pause: null, trackedRunId: 'run-a' })
    const { run, userMessage } = testRun('run-a')
    const createRun = runFactory()
    const api = fakeApi({
      createRun,
      conversations: vi.fn().mockResolvedValue({ items: [conversation, other] }),
      run: vi.fn().mockResolvedValue({ ...run, status: 'running' }),
      messages: vi.fn().mockImplementation(async (id: string) =>
        id === CONVERSATION_ID
          ? { items: [userMessage], active_run_id: run.id, runs: [{ ...run, status: 'running' }] }
          : { items: [], active_run_id: null, runs: [] },
      ),
    })
    const { store, controller, sources } = setup(api)
    await controller.start()
    await flush()
    expect(store.getState().activeConversationId).toBe(other.id)
    expect(sources.map((source) => source.url)).toEqual(['/api/runs/run-a/events'])
    expect(createRun).not.toHaveBeenCalled()

    emit(sources, 'run-a', 'completed')
    await flush()
    expect(createRun).toHaveBeenCalledWith(other.id, {
      text: 'Во втором',
      model: 'claude-sonnet-5-5',
      idempotency_key: 'key-Во втором',
    })
    expect(store.getState().queue).toEqual([])
  })

  it('после перезагрузки с завершённым отслеживаемым заданием применяет правило паузы', async () => {
    writeQueue('test-user', { items: [queueItem('Следующий')], pause: null, trackedRunId: 'run-done' })
    const createRun = runFactory()
    const runRequest = vi.fn().mockResolvedValue({ ...testRun('run-done').run, status: 'cancelled' })
    const { store, controller } = setup(fakeApi({ createRun, run: runRequest }))
    await controller.start()
    await flush()
    expect(store.getState().queuePause).toEqual({ reason: 'cancelled' })
    expect(store.getState().trackedRunId).toBeNull()
    expect(createRun).not.toHaveBeenCalled()
  })

  it('если отслеживаемое задание не удалось проверить при восстановлении, «Продолжить» проверяет его снова', async () => {
    writeQueue('test-user', { items: [queueItem('Следующий')], pause: null, trackedRunId: 'run-a' })
    const createRun = runFactory()
    const runRequest = vi
      .fn()
      .mockRejectedValueOnce(error(0, 'service_unavailable'))
      .mockResolvedValueOnce({ ...testRun('run-a').run, status: 'completed' })
    const { store, controller } = setup(fakeApi({ createRun, run: runRequest }))
    await controller.start()
    await flush()
    expect(store.getState().queuePause).toMatchObject({ reason: 'request_failed' })
    expect(createRun).not.toHaveBeenCalled()

    controller.resumeQueue()
    await flush()
    expect(runRequest).toHaveBeenCalledTimes(2)
    expect(store.getState().trackedRunId).toBe('run-1')
    expect(createRun).toHaveBeenCalledTimes(1)
    expect(store.getState().queue).toEqual([])
  })

  it('пока создаётся первый диалог, следующие сообщения ставятся в очередь этого диалога по порядку с моделью на момент отправки', async () => {
    let resolveConversation: (value: Conversation) => void = () => {}
    const createConversation = vi.fn().mockReturnValue(new Promise((resolve) => (resolveConversation = resolve)))
    const createRun = runFactory()
    const { store, controller } = setup(
      fakeApi({ conversations: vi.fn().mockResolvedValue({ items: [] }), createConversation, createRun }),
    )
    await controller.start()
    const first = controller.send('Первый')
    store.selectModel('claude-sonnet-5-5')
    const second = controller.send('Второй')
    store.selectModel('image')
    const third = controller.send('Третий')
    expect(store.getState().messages[NO_CONVERSATION].map((message) => message.text)).toEqual(['Первый'])

    resolveConversation(conversation)
    expect(await Promise.all([first, second, third])).toEqual([
      { sent: true, conversationId: CONVERSATION_ID },
      { sent: true, conversationId: CONVERSATION_ID },
      { sent: true, conversationId: CONVERSATION_ID },
    ])
    expect(createConversation).toHaveBeenCalledTimes(1)
    expect(createRun.mock.calls.map(([id, body]) => [id, body.text, body.model])).toEqual([
      [CONVERSATION_ID, 'Первый', 'auto'],
    ])
    expect(store.getState().queue.map((item) => [item.conversationId, item.text, item.model])).toEqual([
      [CONVERSATION_ID, 'Второй', 'claude-sonnet-5-5'],
      [CONVERSATION_ID, 'Третий', 'image'],
    ])
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id)).toEqual(['run-1-user'])
  })

  it('при ошибке создания первого диалога убирает временное сообщение', async () => {
    let rejectConversation: (reason: unknown) => void = () => {}
    const createConversation = vi.fn().mockReturnValue(new Promise((_, reject) => (rejectConversation = reject)))
    const { store, controller } = setup(fakeApi({ conversations: vi.fn().mockResolvedValue({ items: [] }), createConversation }))
    await controller.start()
    const sending = controller.send('Первый')
    expect(store.getState().messages[NO_CONVERSATION].map((message) => message.text)).toEqual(['Первый'])
    rejectConversation(error(0, 'service_unavailable'))
    expect(await sending).toEqual({ sent: false, conversationId: null })
    expect(store.getState().messages[NO_CONVERSATION]).toEqual([])
  })

  it('ответ создания диалога после выхода из сеанса не меняет новое состояние', async () => {
    let resolveConversation: (value: Conversation) => void = () => {}
    const createConversation = vi.fn().mockReturnValue(new Promise((resolve) => (resolveConversation = resolve)))
    const renameConversation = vi.fn().mockRejectedValue(error(401, 'unauthorized'))
    const createRun = runFactory()
    const { store, controller } = setup(
      fakeApi({ conversations: vi.fn().mockResolvedValue({ items: [] }), createConversation, renameConversation, createRun }),
    )
    await controller.start()
    const first = controller.send('Первый')
    const second = controller.send('Второй')
    await controller.renameConversation('x', 'y')
    expect(store.getState().session).toBeNull()

    resolveConversation(conversation)
    expect((await first).sent).toBe(false)
    expect((await second).sent).toBe(false)
    expect(store.getState().conversations).toEqual([])
    expect(store.getState().queue).toEqual([])
    expect(store.getState().messages).toEqual({})
    expect(createRun).not.toHaveBeenCalled()
  })

  it.each([
    ['completed', null],
    ['failed', 'failed'],
  ] as const)(
    'повтор с тем же ключом вернул уже завершённое задание (%s): очередь не блокируется',
    async (status, pause) => {
      writeQueue('test-user', {
        items: [queueItem('Принятый'), queueItem('Следующий')],
        pause: null,
        trackedRunId: null,
      })
      const done = testRun('run-done')
      const answer: Message = { ...done.userMessage, id: 'run-done-answer', role: 'assistant', text: 'Ответ' }
      const createRun = vi
        .fn()
        .mockResolvedValueOnce({ ...done.run, status })
        .mockResolvedValueOnce(testRun('run-next').run)
      const messages = vi.fn().mockResolvedValue({
        items: [done.userMessage, answer],
        active_run_id: null,
        runs: [{ ...done.run, status }],
      })
      const { store, controller, sources } = setup(fakeApi({ createRun, messages }))
      await controller.start()
      await flush()
      expect(sources.map((source) => source.url)).not.toContain('/api/runs/run-done/events?after_seq=9007199254740991')
      expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id).slice(0, 2)).toEqual([
        done.userMessage.id,
        'run-done-answer',
      ])
      if (pause) {
        expect(store.getState().queuePause).toEqual({ reason: pause })
        expect(createRun).toHaveBeenCalledTimes(1)
        expect(store.getState().queue.map((item) => item.text)).toEqual(['Следующий'])
      } else {
        expect(store.getState().queuePause).toBeNull()
        expect(createRun).toHaveBeenCalledTimes(2)
        expect(store.getState().queue).toEqual([])
        expect(store.getState().trackedRunId).toBe('run-next')
      }
    },
  )

  it('при недоступном sessionStorage очередь работает в памяти', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    expect(store.getState().queue).toHaveLength(1)
    emit(sources, 'run-1', 'completed')
    await flush()
    expect(createRun).toHaveBeenCalledTimes(2)
  })

  it('при 401 во время отправки из очереди выходит из сеанса и очищает очередь в памяти', async () => {
    const createRun = runFactory()
    const { store, controller, sources } = setup(fakeApi({ createRun }))
    await controller.start()
    await controller.send('Первый')
    await controller.send('Второй')
    createRun.mockRejectedValueOnce(error(401, 'unauthorized'))
    emit(sources, 'run-1', 'completed')
    await flush()
    expect(store.getState().session).toBeNull()
    expect(store.getState().queue).toEqual([])
    expect(store.getState().queuePause).toBeNull()
  })
})
