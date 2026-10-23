import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { createChatStore, type QueuedMessage } from '@/store/conversations'
import { CONVERSATION_ID, testState } from '@/test/fixtures'
import { MessageQueue } from './MessageQueue'

function item(text: string, conversationId = CONVERSATION_ID): QueuedMessage {
  return {
    id: `item-${text}`,
    conversationId,
    text,
    model: 'auto',
    idempotencyKey: `key-${text}`,
    createdAt: '2026-10-24T05:00:00Z',
  }
}

function setup() {
  const state = testState()
  const other = { ...state.conversations[0], id: 'other-conversation', title: 'Другой диалог' }
  const store = createChatStore({ ...state, conversations: [...state.conversations, other] })
  const onRemove = vi.fn((id: string) => store.removeQueued(id))
  const onResume = vi.fn()
  const view = render(<MessageQueue store={store} onRemove={onRemove} onResume={onResume} />)
  return { store, onRemove, onResume, other, view }
}

describe('MessageQueue', () => {
  it('показывает номер, текст и диалог, если он не текущий, и убирает сообщение', () => {
    const { store, onRemove, other } = setup()
    expect(screen.queryByRole('region', { name: 'Очередь сообщений' })).not.toBeInTheDocument()
    act(() => {
      store.enqueue(item('Первый'))
      store.enqueue(item('Второй', other.id))
    })
    const region = screen.getByRole('region', { name: 'Очередь сообщений' })
    expect(within(region).getByText('В очереди: 2 из 5')).toBeInTheDocument()
    const rows = within(region).getAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('1.Первый')
    expect(rows[0]).not.toHaveTextContent('Диалог:')
    expect(rows[1]).toHaveTextContent('2.Второй')
    expect(rows[1]).toHaveTextContent('Диалог: Другой диалог')

    fireEvent.click(screen.getByRole('button', { name: 'Убрать из очереди сообщение 1' }))
    expect(onRemove).toHaveBeenCalledWith('item-Первый')
    expect(within(region).getAllByRole('listitem')).toHaveLength(1)
  })

  it('объявляет добавление и удаление через aria-live', () => {
    const { store, view } = setup()
    const live = view.container.querySelector('[aria-live="polite"]')
    act(() => store.enqueue(item('Первый')))
    expect(live).toHaveTextContent('Сообщение добавлено в очередь. В очереди: 1 из 5.')
    act(() => store.removeQueued('item-Первый'))
    expect(live).toHaveTextContent('Сообщение убрано из очереди. В очереди: 0 из 5.')
  })

  it('на паузе показывает причину и кнопку «Продолжить»', () => {
    const { store, onResume } = setup()
    act(() => {
      store.enqueue(item('Первый'))
      store.pauseQueue({ reason: 'cancelled' })
    })
    expect(screen.getByRole('status')).toHaveTextContent('Очередь приостановлена: ответ остановлен')
    fireEvent.click(screen.getByRole('button', { name: 'Продолжить' }))
    expect(onResume).toHaveBeenCalledTimes(1)

    act(() =>
      store.pauseQueue({
        reason: 'request_failed',
        error: { code: 'quota_exceeded', message: 'Лимит запросов исчерпан.', retryable: false, operation_id: 'op' },
      }),
    )
    expect(screen.getByRole('status')).toHaveTextContent('Очередь приостановлена: Лимит запросов исчерпан.')
  })

  it('отправляемое сообщение нельзя убрать', () => {
    const { store } = setup()
    act(() => {
      store.enqueue(item('Первый'))
      store.setSending(true, 'item-Первый')
    })
    expect(screen.getByText('Отправляется…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Убрать из очереди сообщение 1' })).not.toBeInTheDocument()
  })
})
