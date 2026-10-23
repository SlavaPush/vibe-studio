import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import type { Message } from '@/api/types'
import type { ConversationActions } from '@/store/assistantAdapter'
import { createChatStore, type ChatStore } from '@/store/conversations'
import { draftKey } from '@/store/drafts'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
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

  it('показывает только последнее сообщение об отказе в диалоге', async () => {
    const message = (id: string, role: Message['role'], kind: Message['kind'], text: string): Message => ({
      ...testRun(id).userMessage,
      id,
      role,
      kind,
      text,
    })
    const state = testState()
    const store = createChatStore({
      ...state,
      messages: {
        [CONVERSATION_ID]: [
          message('u1', 'user', null, 'Первый запрос'),
          message('s1', 'system', 'refusal', 'Первый отказ'),
          message('s2', 'system', 'model_switched', 'Модель переключена на резервную'),
          message('u2', 'user', null, 'Второй запрос'),
          message('s3', 'system', 'refusal', 'Второй отказ'),
          message('s4', 'system', 'check_unavailable', 'Проверка недоступна'),
        ],
      },
    })
    render(
      <ChatThread
        store={store}
        actions={{ send: () => {}, cancel: () => {} }}
        loadAssetUrl={() => new Promise(() => {})}
      />,
    )

    const refusals = () => screen.getAllByRole('note', { name: 'Запрос отклонён' })
    expect(await screen.findByText('Второй отказ')).toBeInTheDocument()
    expect(refusals()).toHaveLength(1)
    expect(screen.queryByText('Первый отказ')).not.toBeInTheDocument()
    expect(screen.getByText('Первый запрос')).toBeInTheDocument()
    expect(screen.getByText('Второй запрос')).toBeInTheDocument()
    expect(screen.getByRole('note', { name: 'Модель переключена' })).toBeInTheDocument()
    expect(screen.getByRole('note', { name: 'Проверка недоступна' })).toBeInTheDocument()

    const { run, userMessage } = testRun('run-3')
    act(() => store.startRun(run, { ...userMessage, text: 'Третий запрос' }))
    act(() =>
      store.dispatch({
        event_id: 'run-3-1',
        run_id: 'run-3',
        seq: 1,
        timestamp: '2026-10-23T18:00:01Z',
        type: 'refusal',
        payload: { message: message('s5', 'system', 'refusal', 'Третий отказ') },
      }),
    )

    expect(await screen.findByText('Третий отказ')).toBeInTheDocument()
    expect(refusals()).toHaveLength(1)
    expect(screen.queryByText('Второй отказ')).not.toBeInTheDocument()
    expect(screen.getByText('Третий запрос')).toBeInTheDocument()
  })
})

describe('ChatThread: черновик сообщения', () => {
  afterEach(() => window.localStorage.clear())

  const noActions = { send: () => {}, cancel: () => {} }
  const renderThread = (store: ChatStore, actions: ConversationActions = noActions) =>
    render(
      <StrictMode>
        <ChatThread store={store} actions={actions} loadAssetUrl={() => new Promise(() => {})} />
      </StrictMode>,
    )
  const composer = () => screen.getByRole('textbox', { name: 'Сообщение' })
  const type = (value: string) => fireEvent.change(composer(), { target: { value } })

  it('восстанавливает черновик до создания первого диалога после перезагрузки', async () => {
    const empty = { ...testState(), conversations: [], activeConversationId: null, messages: {}, loaded: {} }
    const first = renderThread(createChatStore(empty))
    type('Черновик до диалога')
    await waitFor(() =>
      expect(window.localStorage.getItem(draftKey('test-user', null))).toBe('Черновик до диалога'),
    )
    first.unmount()

    renderThread(createChatStore(empty))
    await waitFor(() => expect(composer()).toHaveValue('Черновик до диалога'))
  })

  it('хранит отдельный черновик для каждого диалога', async () => {
    const state = testState()
    const other = { ...state.conversations[0], id: 'other-conversation', title: 'Другой' }
    const store = createChatStore({
      ...state,
      conversations: [...state.conversations, other],
      messages: { ...state.messages, [other.id]: [] },
      loaded: { ...state.loaded, [other.id]: true },
    })
    renderThread(store)
    type('Текст первого диалога')
    act(() => store.selectConversation(other.id))
    await waitFor(() => expect(composer()).toHaveValue(''))
    type('Текст второго диалога')
    act(() => store.selectConversation(CONVERSATION_ID))
    await waitFor(() => expect(composer()).toHaveValue('Текст первого диалога'))
    expect(window.localStorage.getItem(draftKey('test-user', other.id))).toBe('Текст второго диалога')
  })

  it('очищает черновик при отправке; диалог, созданный при отправке, начинается без черновика', async () => {
    const empty = { ...testState(), conversations: [], activeConversationId: null, messages: {}, loaded: {} }
    const store = createChatStore(empty)
    const created = { ...testState().conversations[0], id: 'created-conversation' }
    const sent: string[] = []
    renderThread(store, {
      send: (text) => {
        sent.push(text)
        store.addConversation(created)
      },
      cancel: () => {},
    })
    type('Первый запрос')
    await waitFor(() => expect(window.localStorage.getItem(draftKey('test-user', null))).toBe('Первый запрос'))
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))

    await waitFor(() => expect(sent).toEqual(['Первый запрос']))
    await waitFor(() => expect(composer()).toHaveValue(''))
    expect(window.localStorage.getItem(draftKey('test-user', null))).toBeNull()
    expect(window.localStorage.getItem(draftKey('test-user', created.id))).toBeNull()
  })
})
