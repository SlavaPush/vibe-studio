import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError, type ApiClient } from '@/api/client'
import { createChatController, type ChatController } from '@/store/chatController'
import { createChatStore, initialChatState } from '@/store/conversations'
import { draftKey } from '@/store/drafts'
import { watchVersion } from '@/store/versionWatch'
import { CONVERSATION_ID, testRun, testState } from '@/test/fixtures'
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

    expect(await screen.findByText('Доступна новая версия.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Обновить страницу' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Сообщение' })).toBe(composer)
    expect(composer).toHaveValue('Недописанный вопрос')
    expect(window.localStorage.getItem(draftKey('test-user', CONVERSATION_ID))).toBe('Недописанный вопрос')
    stop()
    window.localStorage.clear()
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
})
