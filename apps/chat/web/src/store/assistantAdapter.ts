import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from '@assistant-ui/react'
import { useCallback, useMemo } from 'react'
import type { AssetRef, Effort, Message, ModelId, SystemMessageKind } from '@/api/types'
import { activeRunId, isSendBlocked, useChatState, type ChatStore, type RunState } from './conversations'

export type MessageCustom = {
  kind: SystemMessageKind | null
  effective_model: ModelId | null
  effort: Effort | null
  asset: AssetRef | null
  incomplete: boolean
}

export interface SendResult {
  sent: boolean
  conversationId: string | null
}

export interface ConversationActions {
  send: (text: string) => Promise<SendResult> | void
  cancel: () => void
}

const NO_MESSAGES: Message[] = []

export function toThreadMessage(message: Message, runs: Record<string, RunState> = {}): ThreadMessageLike {
  const status = message.run_id ? runs[message.run_id]?.status : undefined
  const custom: MessageCustom = {
    kind: message.kind,
    effective_model: message.effective_model,
    effort: message.effort,
    asset: message.asset,
    incomplete: message.role === 'assistant' && (status === 'failed' || status === 'cancelled'),
  }
  return {
    id: message.id,
    role: message.role,
    content: [{ type: 'text', text: message.text }],
    createdAt: new Date(message.created_at),
    metadata: { custom },
  }
}

export function readMessageCustom(custom: Record<string, unknown>): Partial<MessageCustom> {
  return custom as Partial<MessageCustom>
}

function messageText(message: AppendMessage): string {
  return message.content
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('\n')
    .trim()
}

function withLastRefusal(messages: Message[]): Message[] {
  const last = messages.findLast((message) => message.kind === 'refusal')
  return last ? messages.filter((message) => message.kind !== 'refusal' || message === last) : messages
}

export function useConversationRuntime(store: ChatStore, actions: ConversationActions) {
  const allMessages = useChatState(store, (state) =>
    state.activeConversationId
      ? (state.messages[state.activeConversationId] ?? NO_MESSAGES)
      : NO_MESSAGES,
  )
  const messages = useMemo(() => withLastRefusal(allMessages), [allMessages])
  const runs = useChatState(store, (state) => state.runs)
  const isRunning = useChatState(store, (state) => activeRunId(state) !== null)
  const isSendDisabled = useChatState(store, isSendBlocked)
  const convertMessage = useCallback((message: Message) => toThreadMessage(message, runs), [runs])

  return useExternalStoreRuntime({
    messages,
    isRunning,
    isSendDisabled,
    convertMessage,
    onNew: async (message) => {
      const text = messageText(message)
      if (text) void actions.send(text)
    },
    onCancel: async () => actions.cancel(),
  })
}
