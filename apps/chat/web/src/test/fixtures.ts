import type { Message, Run, RunEvent } from '@/api/types'
import { contractExamples } from '@/mocks/contract-examples'
import type { ChatState } from '@/store/conversations'

export const CONVERSATION_ID = 'test-conversation'

export function testState(): ChatState {
  return {
    session: { user_id: 'test-user', expires_at: '2026-10-30T18:00:00Z', csrf_token: 'test' },
    conversations: [
      {
        id: CONVERSATION_ID,
        title: 'Тестовый диалог',
        created_at: '2026-10-23T18:00:00Z',
        updated_at: '2026-10-23T18:00:00Z',
      },
    ],
    activeConversationId: CONVERSATION_ID,
    messages: { [CONVERSATION_ID]: [] },
    runs: {},
    errors: {},
    models: contractExamples.ModelCatalog[0].items,
    selectedModel: 'auto',
    updateAvailable: false,
  }
}

export function testRun(id: string): { run: Run; userMessage: Message } {
  const userMessage: Message = {
    id: `${id}-user`,
    conversation_id: CONVERSATION_ID,
    role: 'user',
    kind: null,
    text: 'Расскажи о потоках',
    run_id: id,
    effective_model: null,
    effort: null,
    asset: null,
    created_at: '2026-10-23T18:00:00Z',
  }
  return {
    userMessage,
    run: {
      id,
      conversation_id: CONVERSATION_ID,
      status: 'queued',
      requested_model: 'claude-haiku-5-5',
      effective_model: null,
      effort: null,
      intent: null,
      complexity: null,
      risk: null,
      switch_reason: null,
      user_message_id: userMessage.id,
      assistant_message_id: null,
      error: null,
      created_at: '2026-10-23T18:00:00Z',
      updated_at: '2026-10-23T18:00:00Z',
    },
  }
}

export function switchedRunEvents(runId: string): RunEvent[] {
  const base = (seq: number) => ({
    event_id: `${runId}-${seq}`,
    run_id: runId,
    seq,
    timestamp: `2026-10-23T18:00:0${seq}Z`,
  })
  const message = (role: Message['role'], text: string): Message => ({
    id: `${runId}-${role}`,
    conversation_id: CONVERSATION_ID,
    role,
    kind: role === 'system' ? 'model_switched' : null,
    text,
    run_id: runId,
    effective_model: role === 'assistant' ? 'gpt-6.1-sol' : null,
    effort: role === 'assistant' ? 'high' : null,
    asset: null,
    created_at: '2026-10-23T18:00:05Z',
  })
  return [
    {
      ...base(1),
      type: 'accepted',
      payload: { user_message_id: `${runId}-user`, requested_model: 'claude-haiku-5-5' },
    },
    {
      ...base(2),
      type: 'model_switched',
      payload: {
        requested_model: 'claude-haiku-5-5',
        effective_model: 'gpt-6.1-sol',
        switch_reason: 'provider_fallback',
        message: message(
          'system',
          'Запрос выполнен моделью gpt-6.1-sol, потому что основной провайдер временно недоступен.',
        ),
      },
    },
    { ...base(3), type: 'text_delta', payload: { message_id: `${runId}-assistant`, delta: 'Потоки ' } },
    {
      ...base(4),
      type: 'text_delta',
      payload: { message_id: `${runId}-assistant`, delta: 'разделяют память.' },
    },
    {
      ...base(5),
      type: 'completed',
      payload: { message: message('assistant', 'Потоки разделяют память.') },
    },
  ]
}
