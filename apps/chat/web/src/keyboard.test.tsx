import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ApiClient } from '@/api/client'
import { createChatController } from '@/store/chatController'
import { createChatStore } from '@/store/conversations'
import { switchedRunEvents, testRun, testState } from '@/test/fixtures'
import App from './App'

describe('основной сценарий с клавиатуры', () => {
  it('переход к полю, Shift+Enter не отправляет, Enter отправляет многострочный текст и показывает ответ', async () => {
    const listeners = new Map<string, (event: MessageEvent<string>) => void>()
    const openEvents = () =>
      ({
        addEventListener: (type: string, listener: (event: MessageEvent<string>) => void) => listeners.set(type, listener),
        close: () => {},
      }) as unknown as EventSource
    const { run } = testRun('run-keyboard')
    const createRun = vi.fn().mockResolvedValue(run)
    const api = {
      createRun,
      conversations: vi.fn().mockResolvedValue({ items: testState().conversations }),
    } as unknown as ApiClient
    const store = createChatStore(testState())
    render(<App store={store} controller={createChatController(store, api, openEvents)} />)

    const link = screen.getByRole('link', { name: 'Перейти к полю сообщения' })
    link.focus()
    fireEvent.click(link)
    const composer = screen.getByRole('textbox', { name: 'Сообщение' })
    expect(composer).toHaveFocus()

    const text = 'Первая строка\nВторая строка'
    fireEvent.change(composer, { target: { value: text } })
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true)
    await act(async () => {})
    expect(createRun).not.toHaveBeenCalled()
    expect(composer).toHaveValue(text)

    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
    expect(createRun.mock.calls[0][1].text).toBe(text)
    expect(composer).toHaveValue('')

    await act(async () => {
      listeners.get('completed')?.({ data: JSON.stringify(switchedRunEvents(run.id).at(-1)) } as MessageEvent<string>)
    })
    const log = screen.getByRole('log', { name: 'Сообщения диалога' })
    expect(await within(log).findByText('Потоки разделяют память.')).toBeInTheDocument()
    expect(createRun).toHaveBeenCalledTimes(1)
    window.localStorage.clear()
  })
})
