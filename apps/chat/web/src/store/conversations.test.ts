import { describe, expect, it } from 'vitest'
import type { ApiError, Message, MessageList, Run, RunStatus } from '@/api/types'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
import {
  addRun,
  applyMessageList,
  applyRunEvent,
  createChatStore,
  isIncomplete,
  pendingMessageId,
} from './conversations'

function finishedRun(id: string, status: RunStatus, error: ApiError | null): Run {
  return { ...testRun(id).run, status, error, assistant_message_id: `${id}-assistant` }
}

function apiError(code: ApiError['code'], retryable = false): ApiError {
  return { code, message: `Ошибка ${code}`, retryable, operation_id: 'op_test' }
}

function assistant(runId: string, text: string): Message {
  return { ...testRun(runId).userMessage, id: `${runId}-assistant`, role: 'assistant', text }
}

describe('applyRunEvent', () => {
  it('отбрасывает повтор события по паре run_id и seq, но обрабатывает тот же seq другого задания', () => {
    const first = testRun('run-a')
    const second = testRun('run-b')
    let state = addRun(addRun(testState(), first.run, first.userMessage), second.run, second.userMessage)

    const [, , delta] = switchedRunEvents('run-a')
    const [, , otherDelta] = switchedRunEvents('run-b')
    state = [delta, delta, otherDelta].reduce(applyRunEvent, state)

    const texts = state.messages[CONVERSATION_ID]
      .filter((message) => message.role === 'assistant')
      .map((message) => message.text)
    expect(texts).toEqual(['Потоки ', 'Потоки '])
    expect(state.runs['run-a'].lastSeq).toBe(3)
    expect(state.runs['run-b'].lastSeq).toBe(3)
  })

  it('игнорирует события неизвестного задания', () => {
    const state = testState()
    expect(switchedRunEvents('unknown').reduce(applyRunEvent, state)).toBe(state)
  })
})

describe('временное сообщение пользователя', () => {
  const item = {
    id: 'q1',
    conversationId: CONVERSATION_ID,
    text: 'Расскажи о потоках',
    model: 'auto' as const,
    idempotencyKey: 'key-1',
    createdAt: '2026-10-24T05:00:00Z',
  }

  it('заменяется серверным сообщением на том же месте', () => {
    const store = createChatStore(testState())
    const earlier = { ...testRun('run-0').userMessage, id: 'earlier', text: 'Раньше' }
    store.loadMessages(CONVERSATION_ID, { items: [earlier], active_run_id: null, runs: [] })
    store.addPendingMessage(item)
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id)).toEqual([
      'earlier',
      pendingMessageId(item),
    ])
    const { run, userMessage } = testRun('run-1')
    store.startRun(run, userMessage, pendingMessageId(item))
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id)).toEqual(['earlier', userMessage.id])
  })

  it('при повторе с тем же ключом сохраняет позицию серверного сообщения из истории', () => {
    const store = createChatStore(testState())
    const { run, userMessage } = testRun('run-1')
    const answer = { ...userMessage, id: 'answer', role: 'assistant' as const, text: 'Ответ' }
    store.loadMessages(CONVERSATION_ID, { items: [userMessage, answer], active_run_id: null, runs: [] })
    store.addPendingMessage(item)
    store.startRun(run, userMessage, pendingMessageId(item))
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id)).toEqual([userMessage.id, 'answer'])
  })

  it('сохраняется при перезагрузке истории и удаляется при ошибке', () => {
    const store = createChatStore(testState())
    store.addPendingMessage(item)
    store.loadMessages(CONVERSATION_ID, { items: [], active_run_id: null, runs: [] })
    expect(store.getState().messages[CONVERSATION_ID].map((message) => message.id)).toEqual([pendingMessageId(item)])
    store.removePendingMessage(item)
    expect(store.getState().messages[CONVERSATION_ID]).toEqual([])
  })
})

describe('applyMessageList', () => {
  it('сбрасывает курсор активного задания и воспроизводит события с начала без дублирования', () => {
    const { run, userMessage } = testRun('run-active')
    let state = applyMessageList(testState(), CONVERSATION_ID, {
      items: [userMessage],
      active_run_id: run.id,
      runs: [{ ...run, status: 'running' }],
    })
    expect(state.runs[run.id]).toEqual({ conversationId: CONVERSATION_ID, status: 'running', lastSeq: 0 })
    const events = switchedRunEvents(run.id)
    state = [...events.slice(0, 3), ...events].reduce(applyRunEvent, state)
    const answer = state.messages[CONVERSATION_ID].find((message) => message.role === 'assistant')
    expect(answer?.text).toBe('Потоки разделяют память.')
    expect(state.runs[run.id].status).toBe('completed')
  })

  it.each([
    ['ошибка до первого фрагмента', 'failed', apiError('provider_unavailable', true), ''],
    ['частичный ответ', 'failed', apiError('provider_outcome_unknown'), 'Потоки '],
    ['отмена', 'cancelled', apiError('run_cancelled'), 'Потоки'],
  ] as const)('%s: ошибка последнего задания и отметка незавершённого ответа', (_, status, error, text) => {
    const { userMessage } = testRun('run-x')
    const list: MessageList = {
      items: [userMessage, assistant('run-x', text)],
      active_run_id: null,
      runs: [finishedRun('run-x', status, error)],
    }
    const state = applyMessageList(testState(), CONVERSATION_ID, list)
    expect(state.errors[CONVERSATION_ID]).toEqual(error)
    expect(state.loaded[CONVERSATION_ID]).toBe(true)
    expect(isIncomplete(state, list.items[1])).toBe(true)
    expect(isIncomplete(state, list.items[0])).toBe(false)
    expect(applyRunEvent(state, switchedRunEvents('run-x')[4])).toBe(state)
  })

  it('не показывает ошибку, если последнее задание завершилось успешно', () => {
    const { userMessage } = testRun('run-ok')
    const state = applyMessageList(testState(), CONVERSATION_ID, {
      items: [userMessage],
      active_run_id: null,
      runs: [finishedRun('run-old', 'failed', apiError('provider_unavailable')), finishedRun('run-ok', 'completed', null)],
    })
    expect(state.errors[CONVERSATION_ID]).toBeUndefined()
  })
})
