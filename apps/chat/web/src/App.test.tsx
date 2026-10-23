import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '@/api/client'
import type { ChatController } from '@/store/chatController'
import { createChatStore } from '@/store/conversations'
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
