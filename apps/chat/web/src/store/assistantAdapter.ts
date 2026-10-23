import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from '@assistant-ui/react'
import type { AssetRef, Effort, Message, ModelId, SystemMessageKind } from '@/api/types'
import { activeRunId, useChatState, type ChatStore } from './conversations'

export type MessageCustom = {
  kind: SystemMessageKind | null
  effective_model: ModelId | null
  effort: Effort | null
  asset: AssetRef | null
}

export interface ConversationActions {
  send: (text: string) => void
  cancel: () => void
}

const NO_MESSAGES: Message[] = []

export function toThreadMessage(message: Message): ThreadMessageLike {
  const custom: MessageCustom = {
    kind: message.kind,
    effective_model: message.effective_model,
    effort: message.effort,
    asset: message.asset,
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

export function useConversationRuntime(store: ChatStore, actions: ConversationActions) {
  const messages = useChatState(store, (state) =>
    state.activeConversationId
      ? (state.messages[state.activeConversationId] ?? NO_MESSAGES)
      : NO_MESSAGES,
  )
  const isRunning = useChatState(store, (state) => activeRunId(state) !== null)

  return useExternalStoreRuntime({
    messages,
    isRunning,
    convertMessage: toThreadMessage,
    onNew: async (message) => {
      const text = messageText(message)
      if (text) actions.send(text)
    },
    onCancel: async () => actions.cancel(),
  })
}
