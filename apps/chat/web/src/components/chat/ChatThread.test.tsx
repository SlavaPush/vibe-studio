import { act, fireEvent, render, screen, within } from '@testing-library/react'
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
        loadAssetUrl={() => new Promise(() => {})}
      />,
    )

    act(() => store.startRun(run, userMessage))
    expect(await screen.findByText('Расскажи о потоках')).toBeInTheDocument()

    const [accepted, switched, firstDelta, secondDelta, completed] = switchedRunEvents('run-1')
    act(() => store.dispatch(accepted))
    act(() => store.dispatch(switched))

    act(() => store.dispatch(firstDelta))
    let answers = screen.getAllByRole('article', { name: 'Ответ ассистента' })
    expect(answers).toHaveLength(1)
    expect(within(answers[0]).getByText('Потоки')).toBeInTheDocument()

    act(() => store.dispatch(secondDelta))
    answers = screen.getAllByRole('article', { name: 'Ответ ассистента' })
    expect(answers).toHaveLength(1)
    expect(within(answers[0]).getByText('Потоки разделяют память.')).toBeInTheDocument()

    act(() => store.dispatch(completed))

    const notes = screen.getAllByRole('note')
    expect(notes).toHaveLength(1)
    expect(notes[0]).toHaveAccessibleName('Модель переключена')
    expect(notes[0]).toHaveTextContent('Запрос выполнен моделью gpt-6.1-sol')

    answers = screen.getAllByRole('article', { name: 'Ответ ассистента' })
    expect(answers).toHaveLength(1)
    expect(screen.getAllByText('Потоки разделяют память.')).toHaveLength(1)
    expect(answers[0]).toHaveTextContent('Модель: GPT-6.1 Sol · усилия: высокий')
  })

  it('сохраняет отметку незавершённого ответа после отмены и нового запроса', async () => {
    const store = createChatStore(testState())
    const first = testRun('run-1')
    const second = testRun('run-2')
    const [accepted, , firstDelta] = switchedRunEvents('run-1')
    let cancelled = 0
    render(
      <ChatThread
        store={store}
        actions={{
          send: () => store.startRun(second.run, { ...second.userMessage, text: 'Ещё вопрос' }),
          cancel: () => {
            cancelled += 1
            store.dispatch({
              event_id: 'run-1-4',
              run_id: 'run-1',
              seq: 4,
              timestamp: '2026-10-23T18:00:04Z',
              type: 'failed',
              payload: {
                error: {
                  code: 'run_cancelled',
                  message: 'Задание отменено',
                  retryable: false,
                  operation_id: 'op_test',
                },
              },
            })
          },
        }}
        loadAssetUrl={() => new Promise(() => {})}
      />,
    )

    act(() => store.startRun(first.run, first.userMessage))
    act(() => store.dispatch(accepted))
    act(() => store.dispatch(firstDelta))
    expect(await screen.findByText('Потоки')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Остановить' }))
    expect(await screen.findByText('Ответ не завершён')).toBeInTheDocument()
    expect(cancelled).toBe(1)

    fireEvent.change(screen.getByRole('textbox', { name: 'Сообщение' }), {
      target: { value: 'Ещё вопрос' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(await screen.findByText('Ещё вопрос')).toBeInTheDocument()

    const answers = screen.getAllByRole('article', { name: 'Ответ ассистента' })
    expect(answers[0]).toHaveTextContent('Потоки')
    expect(within(answers[0]).getByText('Ответ не завершён')).toBeInTheDocument()
    expect(screen.getAllByText('Ответ не завершён')).toHaveLength(1)
  })
})
