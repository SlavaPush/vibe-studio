import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError, type ApiClient } from '@/api/client'
import { createChatController, type ChatController } from '@/store/chatController'
import { createChatStore, initialChatState } from '@/store/conversations'
import { draftKey } from '@/store/drafts'
import { watchVersion } from '@/store/versionWatch'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
import App from './App'

function fakeController(overrides: Partial<ChatController> = {}): ChatController {
  return {
    start: vi.fn(),
    signIn: vi.fn(),
    selectConversation: vi.fn(),
    createConversation: vi.fn(),
    renameConversation: vi.fn(),
    send: vi.fn(),
    cancel: vi.fn(),
    removeQueued: vi.fn(),
    resumeQueue: vi.fn(),
    loadAssetUrl: () => new Promise(() => {}),
    ...overrides,
  }
}

describe('App', () => {
  it('после загрузки показывает ошибку последнего задания и отметку незавершённого ответа', async () => {
    const store = createChatStore(testState())
    const { run, userMessage } = testRun('run-partial')
    act(() =>
      store.loadMessages(CONVERSATION_ID, {
        items: [userMessage, { ...userMessage, id: 'answer', role: 'assistant', text: 'Потоки ' }],
        active_run_id: null,
        runs: [
          {
            ...run,
            status: 'failed',
            assistant_message_id: 'answer',
            error: {
              code: 'provider_outcome_unknown',
              message: 'Исход запроса к модели неизвестен.',
              retryable: false,
              operation_id: 'op_partial',
            },
          },
        ],
      }),
    )
    render(<App store={store} controller={fakeController()} />)

    expect(await screen.findByText('Ответ не завершён')).toBeInTheDocument()
    expect(screen.getByText('Не удалось получить ответ')).toBeInTheDocument()
    expect(screen.getByText('Исход запроса к модели неизвестен.')).toBeInTheDocument()
    expect(screen.getByText('Код операции: op_partial')).toBeInTheDocument()
  })

  it('переименовывает диалог по Enter и отменяет по Escape', () => {
    const renameConversation = vi.fn()
    const store = createChatStore(testState())
    render(<App store={store} controller={fakeController({ renameConversation })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Переименовать диалог «Тестовый диалог»' }))
    const input = screen.getByRole('textbox', { name: 'Название диалога' })
    fireEvent.change(input, { target: { value: 'Отменённое' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(renameConversation).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Переименовать диалог «Тестовый диалог»' }))
    const again = screen.getByRole('textbox', { name: 'Название диалога' })
    fireEvent.change(again, { target: { value: 'Кот в скафандре' } })
    fireEvent.keyDown(again, { key: 'Enter' })
    expect(renameConversation).toHaveBeenCalledWith(CONVERSATION_ID, 'Кот в скафандре')
  })

  it('при новой версии сервера показывает уведомление, не перезагружая страницу и сохраняя черновик', async () => {
    const store = createChatStore(testState())
    render(<App store={store} controller={fakeController()} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Недописанный вопрос' } })

    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ version: 'v2' }), { status: 200 }))
    const stop = watchVersion('v1', store.showUpdate, fetchImpl)
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    const dialog = await screen.findByRole('dialog', { name: 'Доступна новая версия' })
    expect(dialog).toHaveAccessibleDescription(
      'Обновите страницу, чтобы получить изменения. Черновик сообщения и история диалогов сохранятся.',
    )
    expect(within(dialog).getByRole('button', { name: 'Обновить страницу' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Позже' })).toHaveFocus()
    expect(screen.queryByText('Доступна новая версия.')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Сообщение', hidden: true })).toBe(composer)
    expect(composer).toHaveValue('Недописанный вопрос')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Позже' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByText('Доступна новая версия.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Сообщение' })).toBe(composer)
    expect(composer).toHaveValue('Недописанный вопрос')
    expect(window.localStorage.getItem(draftKey('test-user', CONVERSATION_ID))).toBe('Недописанный вопрос')
    stop()
    window.localStorage.clear()
  })

  it.each([
    ['кнопкой «Позже»', (dialog: HTMLElement) => fireEvent.click(within(dialog).getByRole('button', { name: 'Позже' }))],
    ['по Escape', (dialog: HTMLElement) => fireEvent.keyDown(dialog, { key: 'Escape' })],
  ])('окно новой версии закрывается %s, возвращает фокус и больше не открывается', async (_, close) => {
    const store = createChatStore(testState())
    render(<App store={store} controller={fakeController()} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    composer.focus()
    act(() => store.showUpdate())

    const dialog = await screen.findByRole('dialog', { name: 'Доступна новая версия' })
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Позже' })).toHaveFocus())
    close(dialog)

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(composer).toHaveFocus()
    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('Доступна новая версия.')
    expect(within(notice).getByRole('button', { name: 'Обновить страницу' })).toBeInTheDocument()

    act(() => store.showUpdate())
    const { run, userMessage } = testRun('run-after-dismiss')
    act(() => store.startRun(run, userMessage))
    act(() => store.dispatch(switchedRunEvents(run.id).at(-1)!))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each(['queued', 'running'] as const)(
    'при задании в статусе %s окно новой версии ждёт завершения задания',
    async (status) => {
      const store = createChatStore(testState())
      render(<App store={store} controller={fakeController()} />)
      const { run, userMessage } = testRun(`run-${status}`)
      act(() => {
        if (status === 'queued') {
          store.startRun(run, userMessage)
        } else {
          store.loadMessages(CONVERSATION_ID, {
            items: [userMessage],
            active_run_id: run.id,
            runs: [{ ...run, status }],
          })
        }
      })
      act(() => store.showUpdate())

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(screen.getByText('Доступна новая версия.')).toBeInTheDocument()

      act(() => store.dispatch(switchedRunEvents(run.id).at(-1)!))
      expect(await screen.findByRole('dialog', { name: 'Доступна новая версия' })).toBeInTheDocument()
    },
  )

  it('пока сообщение отправляется, окно новой версии не открывается', async () => {
    const store = createChatStore(testState())
    render(<App store={store} controller={fakeController()} />)
    act(() => store.setSending(true))
    act(() => store.showUpdate())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    act(() => store.setSending(false))
    expect(await screen.findByRole('dialog', { name: 'Доступна новая версия' })).toBeInTheDocument()
  })

  it('кнопка «Обновить страницу» в окне новой версии перезагружает страницу', async () => {
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })
    try {
      const store = createChatStore(testState())
      render(<App store={store} controller={fakeController()} />)
      act(() => store.showUpdate())
      const dialog = await screen.findByRole('dialog', { name: 'Доступна новая версия' })
      expect(reload).not.toHaveBeenCalled()
      fireEvent.click(within(dialog).getByRole('button', { name: 'Обновить страницу' }))
      expect(reload).toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('после перезагрузки восстанавливает выбранный диалог и его черновик', async () => {
    const first = testState().conversations[0]
    const other = { ...first, id: 'other-conversation', title: 'Другой' }
    const api = {
      models: vi.fn().mockResolvedValue({ items: [] }),
      me: vi.fn().mockResolvedValue(testState().session),
      conversations: vi.fn().mockResolvedValue({ items: [first, other] }),
      messages: vi.fn().mockResolvedValue({ items: [], active_run_id: null, runs: [] }),
    } as unknown as ApiClient
    const open = async () => {
      const store = createChatStore(initialChatState())
      const controller = createChatController(store, api)
      const view = render(<App store={store} controller={controller} />)
      await act(() => controller.start())
      return view
    }

    const page = await open()
    await act(() => fireEvent.click(screen.getByRole('button', { name: 'Другой' })))
    fireEvent.change(screen.getByRole('textbox', { name: 'Сообщение' }), { target: { value: 'Черновик второго' } })
    await waitFor(() =>
      expect(window.localStorage.getItem(draftKey('test-user', other.id))).toBe('Черновик второго'),
    )
    page.unmount()

    await open()
    expect(screen.getByRole('heading', { name: 'Другой' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Сообщение' })).toHaveValue('Черновик второго'))
    window.localStorage.clear()
  })

  it('до ответа /api/me показывает загрузку, а после 401 — экран входа', async () => {
    let reject: (reason: unknown) => void = () => {}
    const api = {
      models: vi.fn().mockResolvedValue({ items: [] }),
      me: vi.fn().mockReturnValue(new Promise((_, fail) => (reject = fail))),
      forgetSession: vi.fn(),
    } as unknown as ApiClient
    const store = createChatStore(initialChatState())
    const controller = createChatController(store, api)
    render(<App store={store} controller={controller} />)
    const starting = controller.start()

    expect(screen.getByRole('status')).toHaveTextContent('Загрузка…')
    expect(screen.queryByLabelText('Код доступа')).not.toBeInTheDocument()

    await act(async () => {
      reject(new ApiRequestError({ code: 'unauthorized', message: 'нет', retryable: false, operation_id: 'op' }, 401))
      await starting
    })
    expect(screen.getByLabelText('Код доступа')).toBeInTheDocument()
    expect(screen.queryByText('Загрузка…')).not.toBeInTheDocument()
  })

  it('после успешного /api/me не показывает экран входа и пустой диалог до загрузки сообщений', async () => {
    const conversation = testState().conversations[0]
    let resolveMessages: (list: unknown) => void = () => {}
    const api = {
      models: vi.fn().mockResolvedValue({ items: [] }),
      me: vi.fn().mockResolvedValue(testState().session),
      conversations: vi.fn().mockResolvedValue({ items: [conversation] }),
      messages: vi.fn().mockReturnValue(new Promise((resolve) => (resolveMessages = resolve))),
    } as unknown as ApiClient
    const store = createChatStore(initialChatState())
    const controller = createChatController(store, api)
    const seen: string[] = []
    const view = render(<App store={store} controller={controller} />)
    const record = () => seen.push(view.container.textContent ?? '')
    const unsubscribe = store.subscribe(record)
    record()

    let starting: Promise<void> = Promise.resolve()
    await act(async () => {
      starting = controller.start()
    })
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Тестовый диалог' })).toBeInTheDocument())
    expect(screen.getByRole('status')).toHaveTextContent('Загрузка сообщений…')
    expect(screen.queryByText('Сообщений пока нет. Напишите первый запрос.')).not.toBeInTheDocument()

    await act(async () => {
      resolveMessages({ items: [], active_run_id: null, runs: [] })
      await starting
    })
    record()
    unsubscribe()
    expect(screen.getByText('Сообщений пока нет. Напишите первый запрос.')).toBeInTheDocument()
    expect(seen.some((text) => text.includes('Вход в vibe-chat'))).toBe(false)
    expect(seen.slice(0, -1).some((text) => text.includes('Сообщений пока нет'))).toBe(false)
  })

  it('при сетевой ошибке /api/me показывает ошибку и повторяет проверку', async () => {
    const me = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiRequestError(
          { code: 'service_unavailable', message: 'Нет соединения с сервером.', retryable: true, operation_id: 'network' },
          0,
        ),
      )
      .mockResolvedValueOnce(testState().session)
    const api = {
      models: vi.fn().mockResolvedValue({ items: [] }),
      me,
      conversations: vi.fn().mockResolvedValue({ items: [] }),
    } as unknown as ApiClient
    const store = createChatStore(initialChatState())
    const controller = createChatController(store, api)
    render(<App store={store} controller={controller} />)
    await act(() => controller.start())

    expect(screen.getByRole('alert')).toHaveTextContent('Нет соединения с сервером.')
    expect(screen.queryByLabelText('Код доступа')).not.toBeInTheDocument()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Повторить' })))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Сообщение' })).toBeInTheDocument())
    expect(me).toHaveBeenCalledTimes(2)
  })

  it('при активном задании Enter и кнопка «В очередь» передают сообщение и очищают поле', async () => {
    const send = vi.fn().mockResolvedValue({ sent: true, conversationId: CONVERSATION_ID })
    const other = { ...testState().conversations[0], id: 'other-conversation', title: 'Другой' }
    const state = testState()
    const store = createChatStore({
      ...state,
      conversations: [...state.conversations, other],
      messages: { ...state.messages, [other.id]: [] },
      loaded: { ...state.loaded, [other.id]: true },
    })
    render(<App store={store} controller={fakeController({ send })} />)
    const { run, userMessage } = testRun('run-busy')
    act(() => store.startRun(run, userMessage))

    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Ещё вопрос' } })
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(send).toHaveBeenCalledWith('Ещё вопрос'))
    expect(composer).toHaveValue('')

    fireEvent.change(composer, { target: { value: 'Третий вопрос' } })
    fireEvent.click(screen.getByRole('button', { name: 'В очередь' }))
    await waitFor(() => expect(send).toHaveBeenCalledWith('Третий вопрос'))
    expect(composer).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Остановить' })).toBeInTheDocument()

    act(() => store.selectConversation(other.id))
    const otherComposer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(otherComposer, { target: { value: 'Вопрос в другом диалоге' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    await waitFor(() => expect(send).toHaveBeenCalledWith('Вопрос в другом диалоге'))
    window.localStorage.clear()
  })

  it('до загрузки истории диалога не отправляет сообщение, а после загрузки с активным заданием ставит его в очередь', async () => {
    const send = vi.fn().mockResolvedValue({ sent: true, conversationId: CONVERSATION_ID })
    const store = createChatStore({ ...testState(), messages: {}, loaded: {} })
    render(<App store={store} controller={fakeController({ send })} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Ранний вопрос' } })
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled()
    await act(async () => {})
    expect(composer).toHaveValue('Ранний вопрос')
    expect(send).not.toHaveBeenCalled()

    const { run, userMessage } = testRun('run-history')
    act(() =>
      store.loadMessages(CONVERSATION_ID, {
        items: [userMessage],
        active_run_id: run.id,
        runs: [{ ...run, status: 'running' }],
      }),
    )
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(send).toHaveBeenCalledWith('Ранний вопрос'))
    expect(composer).toHaveValue('')
    window.localStorage.clear()
  })

  it('ошибка отправки из очереди не трогает поле: сообщение остаётся в очереди с тем же ключом до «Продолжить»', async () => {
    const listeners = new Map<string, (event: MessageEvent<string>) => void>()
    const openEvents = () =>
      ({
        addEventListener: (type: string, listener: (event: MessageEvent<string>) => void) => listeners.set(type, listener),
        close: () => {},
      }) as unknown as EventSource
    const { run } = testRun('run-1')
    let rejectQueued: (reason: unknown) => void = () => {}
    const createRun = vi
      .fn()
      .mockResolvedValueOnce(run)
      .mockReturnValueOnce(new Promise((_, fail) => (rejectQueued = fail)))
      .mockResolvedValueOnce(testRun('run-2').run)
    const api = {
      createRun,
      conversations: vi.fn().mockResolvedValue({ items: testState().conversations }),
    } as unknown as ApiClient
    const store = createChatStore(testState())
    render(<App store={store} controller={createChatController(store, api, openEvents)} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    const log = screen.getByRole('log', { name: 'Сообщения диалога' })

    fireEvent.change(composer, { target: { value: 'Первый' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    await waitFor(() => expect(store.getState().runs[run.id]).toBeDefined())
    fireEvent.change(composer, { target: { value: 'Второй' } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    await waitFor(() => expect(store.getState().queue).toHaveLength(1))
    const key = store.getState().queue[0].idempotencyKey
    fireEvent.change(composer, { target: { value: 'Черновик' } })

    await act(async () => {
      listeners.get('completed')?.({ data: JSON.stringify(switchedRunEvents(run.id).at(-1)) } as MessageEvent<string>)
    })
    expect(await within(log).findByText('Второй')).toBeInTheDocument()
    await act(async () => {
      rejectQueued(
        new ApiRequestError(
          { code: 'rate_limited', message: 'Дождитесь завершения текущего ответа.', retryable: true, operation_id: 'op' },
          429,
        ),
      )
    })

    expect(within(log).queryByText('Второй')).not.toBeInTheDocument()
    expect(composer).toHaveValue('Черновик')
    const queue = screen.getByRole('region', { name: 'Очередь сообщений' })
    expect(within(queue).getByText('Второй')).toBeInTheDocument()
    expect(within(queue).getByRole('status')).toHaveTextContent(
      'Очередь приостановлена: Дождитесь завершения текущего ответа.',
    )
    expect(within(queue).getByRole('button', { name: 'Убрать из очереди сообщение 1' })).toBeInTheDocument()
    expect(store.getState().queue[0].idempotencyKey).toBe(key)

    await act(async () => fireEvent.click(within(queue).getByRole('button', { name: 'Продолжить' })))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(3))
    expect(createRun.mock.calls[2][1]).toEqual(createRun.mock.calls[1][1])
    expect(createRun.mock.calls[2][1].idempotency_key).toBe(key)
    expect(composer).toHaveValue('Черновик')
    window.localStorage.clear()
  })

  it('при полной очереди кнопка и Enter недоступны, текст остаётся в поле, причина видна', async () => {
    const send = vi.fn()
    const items = Array.from({ length: 5 }, (_, index) => ({
      id: `q${index}`,
      conversationId: CONVERSATION_ID,
      text: `Сообщение ${index + 1}`,
      model: 'auto' as const,
      idempotencyKey: `key-${index}`,
      createdAt: '2026-10-24T05:00:00Z',
    }))
    const store = createChatStore({ ...testState(), queue: items })
    render(<App store={store} controller={fakeController({ send })} />)
    const { run, userMessage } = testRun('run-busy')
    act(() => store.startRun(run, userMessage))
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Шестое' } })
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    expect(screen.getByRole('button', { name: 'В очередь' })).toBeDisabled()
    await act(async () => {})
    expect(send).not.toHaveBeenCalled()
    expect(composer).toHaveValue('Шестое')
    expect(screen.getByText(/Очередь заполнена: не больше 5 сообщений/)).toBeInTheDocument()
    window.localStorage.clear()
  })

  it('окно новой версии не открывается, пока очередь не пуста', async () => {
    const item = {
      id: 'q1',
      conversationId: CONVERSATION_ID,
      text: 'В очереди',
      model: 'auto' as const,
      idempotencyKey: 'key-1',
      createdAt: '2026-10-24T05:00:00Z',
    }
    const store = createChatStore({ ...testState(), queue: [item] })
    render(<App store={store} controller={fakeController({ removeQueued: store.removeQueued })} />)
    act(() => store.showUpdate())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText('Доступна новая версия.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Убрать из очереди сообщение 1' }))
    expect(await screen.findByRole('dialog', { name: 'Доступна новая версия' })).toBeInTheDocument()
  })

  it('при ответе 429 на отправку возвращает текст в поле и черновик и показывает ошибку', async () => {
    const createRun = vi.fn().mockRejectedValue(
      new ApiRequestError(
        { code: 'rate_limited', message: 'Дождитесь завершения текущего ответа.', retryable: true, operation_id: 'op' },
        429,
      ),
    )
    const api = { createRun } as unknown as ApiClient
    const store = createChatStore(testState())
    render(<App store={store} controller={createChatController(store, api)} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Вопрос при чужом задании' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(await screen.findByText('Дождитесь завершения текущего ответа.')).toBeInTheDocument()
    await waitFor(() => expect(composer).toHaveValue('Вопрос при чужом задании'))
    expect(createRun).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(window.localStorage.getItem(draftKey('test-user', CONVERSATION_ID))).toBe('Вопрос при чужом задании'),
    )
    window.localStorage.clear()
  })

  it('при отказе после смены диалога возвращает текст в черновик исходного диалога, не меняя новый ввод', async () => {
    let reject: (reason: unknown) => void = () => {}
    const createRun = vi.fn().mockReturnValue(new Promise((_, fail) => (reject = fail)))
    const other = { ...testState().conversations[0], id: 'other-conversation', title: 'Другой' }
    const state = testState()
    const store = createChatStore({
      ...state,
      conversations: [...state.conversations, other],
      messages: { ...state.messages, [other.id]: [] },
      loaded: { ...state.loaded, [other.id]: true },
    })
    render(<App store={store} controller={createChatController(store, { createRun } as unknown as ApiClient)} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Вопрос первого диалога' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))

    act(() => store.selectConversation(other.id))
    const otherComposer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(otherComposer, { target: { value: 'Ввод второго диалога' } })
    await act(async () => {
      reject(
        new ApiRequestError(
          { code: 'rate_limited', message: 'Дождитесь завершения текущего ответа.', retryable: true, operation_id: 'op' },
          429,
        ),
      )
    })
    expect(otherComposer).toHaveValue('Ввод второго диалога')
    expect(window.localStorage.getItem(draftKey('test-user', CONVERSATION_ID))).toBe('Вопрос первого диалога')

    act(() => store.selectConversation(CONVERSATION_ID))
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Сообщение' })).toHaveValue('Вопрос первого диалога'),
    )
    expect(screen.getByText('Дождитесь завершения текущего ответа.')).toBeInTheDocument()
    window.localStorage.clear()
  })

  it('при отказе в том же диалоге сохраняет и неотправленный текст, и новый ввод', async () => {
    let reject: (reason: unknown) => void = () => {}
    const createRun = vi.fn().mockReturnValue(new Promise((_, fail) => (reject = fail)))
    const store = createChatStore(testState())
    render(<App store={store} controller={createChatController(store, { createRun } as unknown as ApiClient)} />)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Первый' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
    const log = screen.getByRole('log', { name: 'Сообщения диалога' })
    expect(await within(log).findByText('Первый')).toBeInTheDocument()
    fireEvent.change(composer, { target: { value: 'Второй' } })
    await act(async () => {
      reject(
        new ApiRequestError({ code: 'internal_error', message: 'Ошибка сервера.', retryable: true, operation_id: 'op' }, 500),
      )
    })
    expect(composer).toHaveValue('Первый\nВторой')
    expect(screen.getByText('Ошибка сервера.')).toBeInTheDocument()
    expect(within(log).queryByText('Первый')).not.toBeInTheDocument()
    window.localStorage.clear()
  })

  it('если диалог для первого сообщения не создан, возвращает текст в поле и показывает ошибку', async () => {
    const createConversation = vi.fn().mockRejectedValue(
      new ApiRequestError(
        { code: 'service_unavailable', message: 'Нет соединения с сервером.', retryable: true, operation_id: 'network' },
        0,
      ),
    )
    const store = createChatStore({ ...testState(), conversations: [], activeConversationId: null, messages: {}, loaded: {} })
    render(
      <App store={store} controller={createChatController(store, { createConversation } as unknown as ApiClient)} />,
    )
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    fireEvent.change(composer, { target: { value: 'Первый вопрос' } })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    expect(await screen.findByText('Нет соединения с сервером.')).toBeInTheDocument()
    await waitFor(() => expect(composer).toHaveValue('Первый вопрос'))
    window.localStorage.clear()
  })

  it('без сеанса показывает вход и ошибку неверного кода', async () => {
    const signIn = vi.fn().mockRejectedValue(
      new ApiRequestError(
        { code: 'invalid_demo_code', message: 'Неверный код доступа.', retryable: false, operation_id: 'op' },
        401,
      ),
    )
    const store = createChatStore({ ...testState(), session: null })
    render(<App store={store} controller={fakeController({ signIn })} />)
    fireEvent.change(screen.getByLabelText('Код доступа'), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный код доступа.')
    expect(signIn).toHaveBeenCalledWith('wrong')
  })

  it('экран входа объясняет одноразовые коды доступа', () => {
    const store = createChatStore({ ...testState(), session: null })
    render(<App store={store} controller={fakeController()} />)
    const info = screen.getByRole('region', { name: 'Как работает доступ' })
    expect(info).toHaveTextContent('демонстрационный проект')
    expect(info).toHaveTextContent('одноразовые коды доступа')
    expect(info).toHaveTextContent('Код действует для одного входа.')
    expect(info).toHaveTextContent('нужен новый код')
    expect(info).toHaveTextContent('Прежняя история будет недоступна.')
  })
})
