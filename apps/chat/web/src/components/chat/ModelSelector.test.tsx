import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { createChatStore, useChatState, type ChatStore } from '@/store/conversations'
import { testState } from '@/test/fixtures'
import { ModelSelector } from './ModelSelector'

function ConnectedSelector({ store }: { store: ChatStore }) {
  const models = useChatState(store, (state) => state.models)
  const value = useChatState(store, (state) => state.selectedModel)
  return <ModelSelector models={models} value={value} onChange={store.selectModel} />
}

describe('ModelSelector', () => {
  it('меняет выбранную модель с клавиатуры', async () => {
    const store = createChatStore(testState())
    render(<ConnectedSelector store={store} />)

    const trigger = screen.getByRole('combobox', { name: 'Модель' })
    expect(trigger).toHaveTextContent('Авто')

    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    const auto = await screen.findByRole('option', { name: 'Авто' })
    await waitFor(() => expect(auto).toHaveFocus())
    fireEvent.keyDown(auto, { key: 'ArrowDown' })
    const haiku = screen.getByRole('option', { name: 'Claude Haiku 5.5' })
    await waitFor(() => expect(haiku).toHaveFocus())
    fireEvent.keyDown(haiku, { key: 'Enter' })

    expect(store.getState().selectedModel).toBe('claude-haiku-5-5')
    expect(screen.getByRole('combobox', { name: 'Модель' })).toHaveTextContent('Claude Haiku 5.5')
  })
})
