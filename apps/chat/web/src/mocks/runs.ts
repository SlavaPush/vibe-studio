import type {
  Message,
  ModelChoice,
  ModelId,
  Run,
  RunEvent,
  RunEventBase,
  SystemMessageKind,
} from '@/api/types'
import { activeRunId, type ChatStore } from '@/store/conversations'

export type MockOutcome = 'text' | 'image' | 'refusal' | 'check_unavailable' | 'fallback' | 'failed'

export interface MockRunInput {
  runId: string
  conversationId: string
  text: string
  requestedModel: ModelChoice
  outcome: MockOutcome
  at: Date
  answer?: string
}

const MOCK_ANSWER =
  'Это тестовый ответ без обращения к модели. На этапе P2 здесь появится ответ выбранной модели.'
const IMAGE_MODEL: ModelId = 'gpt-image-2.5-flare'

export function mockOutcome(text: string, model: ModelChoice): MockOutcome {
  return model === 'image' || /нарису|изображ|картин/i.test(text) ? 'image' : 'text'
}

export function buildMockRun(input: MockRunInput): {
  run: Run
  userMessage: Message
  events: RunEvent[]
} {
  const { runId, conversationId, requestedModel, outcome, at } = input
  const created = at.toISOString()
  const answerId = `${runId}-answer`
  let seq = 0
  const base = (): RunEventBase => {
    seq += 1
    return {
      event_id: `${runId}-${seq}`,
      run_id: runId,
      seq,
      timestamp: new Date(at.getTime() + seq * 1000).toISOString(),
    }
  }
  const message = (role: Message['role'], kind: SystemMessageKind | null, text: string): Message => ({
    id: `${runId}-${kind ?? role}`,
    conversation_id: conversationId,
    role,
    kind,
    text,
    run_id: runId,
    effective_model: null,
    effort: null,
    asset: null,
    created_at: new Date(at.getTime() + (seq + 1) * 1000).toISOString(),
  })

  const userMessage = message('user', null, input.text)
  const run: Run = {
    id: runId,
    conversation_id: conversationId,
    status: 'queued',
    requested_model: requestedModel,
    effective_model: null,
    effort: null,
    intent: null,
    complexity: null,
    risk: null,
    switch_reason: null,
    user_message_id: userMessage.id,
    assistant_message_id: null,
    error: null,
    created_at: created,
    updated_at: created,
  }

  const events: RunEvent[] = [
    {
      ...base(),
      type: 'accepted',
      payload: { user_message_id: userMessage.id, requested_model: requestedModel },
    },
  ]

  if (outcome === 'refusal') {
    events.push({
      ...base(),
      type: 'refusal',
      payload: {
        message: message('system', 'refusal', 'Этот запрос нельзя выполнить: он нарушает правила сервиса.'),
      },
    })
    return { run, userMessage, events }
  }
  if (outcome === 'check_unavailable') {
    events.push({
      ...base(),
      type: 'check_unavailable',
      payload: {
        message: message('system', 'check_unavailable', 'Проверка запроса недоступна, повторите позже.'),
      },
    })
    return { run, userMessage, events }
  }

  events.push({
    ...base(),
    type: 'checks_passed',
    payload: { intent: outcome === 'image' ? 'image' : 'chat', complexity: 'standard', risk: 'none' },
  })

  if (outcome === 'failed') {
    events.push({
      ...base(),
      type: 'failed',
      payload: {
        error: {
          code: 'provider_unavailable',
          message: 'Модели временно недоступны. Повторите запрос позже.',
          retryable: true,
          operation_id: `op_mock_${runId}`,
        },
      },
    })
    return { run, userMessage, events }
  }

  if (outcome === 'image') {
    if (requestedModel !== 'auto' && requestedModel !== 'image') {
      events.push({
        ...base(),
        type: 'model_switched',
        payload: {
          requested_model: requestedModel,
          effective_model: IMAGE_MODEL,
          switch_reason: 'intent_mismatch',
          message: message(
            'system',
            'model_switched',
            `Запрос выполнен моделью ${IMAGE_MODEL}, потому что выбранная модель не генерирует изображения.`,
          ),
        },
      })
    }
    const asset = { id: `${runId}-asset`, mime_type: 'image/svg+xml', width: 512, height: 512 }
    events.push(
      {
        ...base(),
        type: 'model_selected',
        payload: {
          assistant_message_id: answerId,
          provider: 'openai',
          effective_model: IMAGE_MODEL,
          effort: 'unsupported',
        },
      },
      { ...base(), type: 'asset_ready', payload: { message_id: answerId, asset } },
    )
    events.push({
      ...base(),
      type: 'completed',
      payload: {
        message: {
          ...message('assistant', null, ''),
          id: answerId,
          effective_model: IMAGE_MODEL,
          effort: 'unsupported',
          asset,
        },
      },
    })
    return { run, userMessage, events }
  }

  let effectiveModel: ModelId = requestedModel === 'auto' || requestedModel === 'image' ? 'claude-sonnet-5-5' : requestedModel
  let effort: 'medium' | 'high' = 'medium'
  if (outcome === 'fallback') {
    effectiveModel = 'gpt-6.1-sol'
    effort = 'high'
    events.push({
      ...base(),
      type: 'model_switched',
      payload: {
        requested_model: requestedModel,
        effective_model: effectiveModel,
        switch_reason: 'provider_fallback',
        message: message(
          'system',
          'model_switched',
          `Запрос выполнен моделью ${effectiveModel}, потому что основной провайдер временно недоступен.`,
        ),
      },
    })
  }

  events.push({
    ...base(),
    type: 'model_selected',
    payload: {
      assistant_message_id: answerId,
      provider: effectiveModel.startsWith('claude') ? 'anthropic' : 'openai',
      effective_model: effectiveModel,
      effort,
    },
  })
  const answer = input.answer ?? MOCK_ANSWER
  const words = answer.split(' ')
  const third = Math.ceil(words.length / 3)
  for (let i = 0; i < words.length; i += third) {
    const delta = words.slice(i, i + third).join(' ') + (i + third < words.length ? ' ' : '')
    events.push({ ...base(), type: 'text_delta', payload: { message_id: answerId, delta } })
  }
  events.push({
    ...base(),
    type: 'completed',
    payload: {
      message: {
        ...message('assistant', null, answer),
        id: answerId,
        effective_model: effectiveModel,
        effort,
      },
    },
  })
  return { run, userMessage, events }
}

const timers = new Map<string, number[]>()

export function sendMockMessage(store: ChatStore, text: string): void {
  const state = store.getState()
  if (!state.activeConversationId) return
  const { run, userMessage, events } = buildMockRun({
    runId: crypto.randomUUID(),
    conversationId: state.activeConversationId,
    text,
    requestedModel: state.selectedModel,
    outcome: mockOutcome(text, state.selectedModel),
    at: new Date(),
  })
  store.startRun(run, userMessage)
  timers.set(
    run.id,
    events.map((event, i) => window.setTimeout(() => store.dispatch(event), 600 * (i + 1))),
  )
}

export function cancelMockRun(store: ChatStore): void {
  const state = store.getState()
  const runId = activeRunId(state)
  if (!runId) return
  timers.get(runId)?.forEach((timer) => window.clearTimeout(timer))
  timers.delete(runId)
  const seq = state.runs[runId].lastSeq + 1
  store.dispatch({
    event_id: `${runId}-${seq}`,
    run_id: runId,
    seq,
    type: 'failed',
    timestamp: new Date().toISOString(),
    payload: {
      error: {
        code: 'run_cancelled',
        message: 'Запрос отменён.',
        retryable: false,
        operation_id: `op_mock_${runId}`,
      },
    },
  })
}
