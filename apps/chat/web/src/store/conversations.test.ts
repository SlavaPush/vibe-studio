import { describe, expect, it } from 'vitest'
import { CONVERSATION_ID, switchedRunEvents, testRun, testState } from '@/test/fixtures'
import { addRun, applyRunEvent } from './conversations'

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
