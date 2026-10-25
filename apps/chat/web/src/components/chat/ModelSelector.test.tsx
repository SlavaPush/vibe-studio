import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ModelProfile } from '@/api/types'
import { contractExamples } from '@/mocks/contract-examples'
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

  it('показывает «Авто», выбираемые текстовые модели каталога с их подписями и генерацию изображения', async () => {
    const catalog: ModelProfile[] = [
      ...contractExamples.ModelCatalog[0].items,
      { id: 'gpt-6-luna', provider: 'openai', label: 'Скрытая модель', kind: 'text', selectable: false },
    ]
    const store = createChatStore({ ...testState(), models: catalog })
    render(<ConnectedSelector store={store} />)

    const trigger = screen.getByRole('combobox', { name: 'Модель' })
    expect(trigger).toHaveTextContent('Авто')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    await screen.findByRole('option', { name: 'Авто' })

    const textLabels = catalog
      .filter((model) => model.kind === 'text' && model.selectable)
      .map((model) => model.label)
    expect(textLabels.length).toBeGreaterThan(0)
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Авто',
      ...textLabels,
      'Генерация изображения',
    ])
    expect(screen.queryByRole('option', { name: 'Скрытая модель' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'GPT Image 2.5 Flare' })).not.toBeInTheDocument()
  })

  it('поле выбора и пункты списка имеют сплошное кольцо фокуса', async () => {
    const store = createChatStore(testState())
    render(<ConnectedSelector store={store} />)

    const trigger = screen.getByRole('combobox', { name: 'Модель' })
    expect(trigger).toHaveClass('focus-visible:ring-ring')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    const auto = await screen.findByRole('option', { name: 'Авто' })
    expect(auto).toHaveClass('focus:ring-ring', 'focus:ring-inset')
  })
})
