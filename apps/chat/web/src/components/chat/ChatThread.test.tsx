import { act, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { createChatStore } from '@/store/conversations'
import { switchedRunEvents, testRun, testState } from '@/test/fixtures'
import { ChatThread } from './ChatThread'

describe('ChatThread', () => {
  it('показывает события из хранилища: одно системное сообщение о переключении и один ответ с подписью модели', async () => {
    const store = createChatStore(testState())
    const { run, userMessage } = testRun('run-1')
    render(
      <ChatThread
        store={store}
        actions={{ send: () => {}, cancel: () => {} }}
        resolveAssetUrl={() => ''}
      />,
    )

    act(() => store.startRun(run, userMessage))
    expect(await screen.findByText('Расскажи о потоках')).toBeInTheDocument()

    for (const event of switchedRunEvents('run-1')) {
      act(() => store.dispatch(event))
    }

    const notes = screen.getAllByRole('note')
    expect(notes).toHaveLength(1)
    expect(notes[0]).toHaveAccessibleName('Модель переключена')
    expect(notes[0]).toHaveTextContent('Запрос выполнен моделью gpt-6.1-sol')

    const answers = screen.getAllByRole('article', { name: 'Ответ ассистента' })
    expect(answers).toHaveLength(1)
    expect(within(answers[0]).getByText('Потоки разделяют память.')).toBeInTheDocument()
    expect(answers[0]).toHaveTextContent('Модель: GPT-6.1 Sol · усилия: высокий')
  })
})
