import { useSyncExternalStore } from 'react'
import type {
  ApiError,
  Conversation,
  Message,
  MessageList,
  ModelChoice,
  ModelProfile,
  Run,
  RunEvent,
  RunStatus,
  Session,
} from '@/api/types'

export interface RunState {
  conversationId: string
  status: RunStatus
  lastSeq: number
}

export interface ChatState {
  session: Session | null
  conversations: Conversation[]
  activeConversationId: string | null
  messages: Record<string, Message[]>
  loaded: Record<string, boolean>
  runs: Record<string, RunState>
  errors: Record<string, ApiError | undefined>
  models: ModelProfile[]
  selectedModel: ModelChoice
  updateAvailable: boolean
}

const TERMINAL_STATUS: Partial<Record<RunEvent['type'], RunStatus>> = {
  completed: 'completed',
  refusal: 'refused',
  check_unavailable: 'check_unavailable',
}

const FINISHED_SEQ = Number.MAX_SAFE_INTEGER

export function initialChatState(): ChatState {
  return {
    session: null,
    conversations: [],
    activeConversationId: null,
    messages: {},
    loaded: {},
    runs: {},
    errors: {},
    models: [],
    selectedModel: 'auto',
    updateAvailable: false,
  }
}

function upsertMessage(messages: Message[], message: Message): Message[] {
  const index = messages.findIndex((item) => item.id === message.id)
  if (index === -1) return [...messages, message]
  return messages.map((item, i) => (i === index ? message : item))
}

function updateMessage(
  messages: Message[],
  id: string,
  create: () => Message,
  update: (message: Message) => Message,
): Message[] {
  const current = messages.find((item) => item.id === id)
  return upsertMessage(messages, update(current ?? create()))
}

function runStatus(event: RunEvent, current: RunStatus): RunStatus {
  if (event.type === 'failed') {
    return event.payload.error.code === 'run_cancelled' ? 'cancelled' : 'failed'
  }
  return TERMINAL_STATUS[event.type] ?? (current === 'queued' ? 'running' : current)
}

export function applyRunEvent(state: ChatState, event: RunEvent): ChatState {
  const run = state.runs[event.run_id]
  if (!run || event.seq <= run.lastSeq) return state

  const conversationId = run.conversationId
  const messages = state.messages[conversationId] ?? []
  const assistantMessage = (id: string): Message => ({
    id,
    conversation_id: conversationId,
    role: 'assistant',
    kind: null,
    text: '',
    run_id: event.run_id,
    effective_model: null,
    effort: null,
    asset: null,
    created_at: event.timestamp,
  })

  let nextMessages = messages
  let error = state.errors[conversationId]
  switch (event.type) {
    case 'refusal':
    case 'check_unavailable':
    case 'model_switched':
    case 'completed':
      nextMessages = upsertMessage(messages, event.payload.message)
      break
    case 'model_selected': {
      const { assistant_message_id, effective_model, effort } = event.payload
      nextMessages = updateMessage(
        messages,
        assistant_message_id,
        () => assistantMessage(assistant_message_id),
        (message) => ({ ...message, effective_model, effort }),
      )
      break
    }
    case 'text_delta': {
      const { message_id, delta } = event.payload
      nextMessages = updateMessage(
        messages,
        message_id,
        () => assistantMessage(message_id),
        (message) => ({ ...message, text: message.text + delta }),
      )
      break
    }
    case 'asset_ready': {
      const { message_id, asset } = event.payload
      nextMessages = updateMessage(
        messages,
        message_id,
        () => assistantMessage(message_id),
        (message) => ({ ...message, asset }),
      )
      break
    }
    case 'failed':
      error = event.payload.error
      break
    case 'accepted':
    case 'checks_passed':
      break
  }

  return {
    ...state,
    messages: { ...state.messages, [conversationId]: nextMessages },
    runs: {
      ...state.runs,
      [event.run_id]: { ...run, status: runStatus(event, run.status), lastSeq: event.seq },
    },
    errors: { ...state.errors, [conversationId]: error },
  }
}

export function applyMessageList(state: ChatState, conversationId: string, list: MessageList): ChatState {
  const runs = { ...state.runs }
  for (const run of list.runs) {
    runs[run.id] = {
      conversationId,
      status: run.status,
      lastSeq: run.id === list.active_run_id ? 0 : FINISHED_SEQ,
    }
  }
  const last = list.runs.at(-1)
  const error =
    last && (last.status === 'failed' || last.status === 'cancelled') ? (last.error ?? undefined) : undefined
  return {
    ...state,
    messages: { ...state.messages, [conversationId]: list.items },
    loaded: { ...state.loaded, [conversationId]: true },
    runs,
    errors: { ...state.errors, [conversationId]: error },
  }
}

export function addRun(state: ChatState, run: Run, userMessage: Message): ChatState {
  const messages = state.messages[run.conversation_id] ?? []
  return {
    ...state,
    messages: { ...state.messages, [run.conversation_id]: upsertMessage(messages, userMessage) },
    runs: {
      ...state.runs,
      [run.id]: state.runs[run.id] ?? { conversationId: run.conversation_id, status: run.status, lastSeq: 0 },
    },
    errors: { ...state.errors, [run.conversation_id]: undefined },
  }
}

export function createChatStore(initial: ChatState = initialChatState()) {
  let state = initial
  const listeners = new Set<() => void>()
  const set = (next: ChatState) => {
    state = next
    listeners.forEach((listener) => listener())
  }

  return {
    getState: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    dispatch(event: RunEvent) {
      set(applyRunEvent(state, event))
    },
    setModels(models: ModelProfile[]) {
      set({ ...state, models })
    },
    signIn(session: Session) {
      set({ ...state, session })
    },
    signOut() {
      set({ ...initialChatState(), models: state.models, selectedModel: state.selectedModel })
    },
    setConversations(conversations: Conversation[]) {
      set({ ...state, conversations })
    },
    upsertConversation(conversation: Conversation) {
      const exists = state.conversations.some((item) => item.id === conversation.id)
      set({
        ...state,
        conversations: exists
          ? state.conversations.map((item) => (item.id === conversation.id ? conversation : item))
          : [conversation, ...state.conversations],
      })
    },
    selectConversation(id: string) {
      set({ ...state, activeConversationId: id })
    },
    selectModel(model: ModelChoice) {
      set({ ...state, selectedModel: model })
    },
    addConversation(conversation: Conversation) {
      set({
        ...state,
        conversations: [conversation, ...state.conversations],
        activeConversationId: conversation.id,
        messages: { ...state.messages, [conversation.id]: [] },
        loaded: { ...state.loaded, [conversation.id]: true },
      })
    },
    loadMessages(conversationId: string, list: MessageList) {
      set(applyMessageList(state, conversationId, list))
    },
    startRun(run: Run, userMessage: Message) {
      set(addRun(state, run, userMessage))
    },
    setError(conversationId: string, error: ApiError | undefined) {
      set({ ...state, errors: { ...state.errors, [conversationId]: error } })
    },
  }
}

export type ChatStore = ReturnType<typeof createChatStore>

export function useChatState<T>(store: ChatStore, select: (state: ChatState) => T): T {
  return useSyncExternalStore(store.subscribe, () => select(store.getState()))
}

export function activeRunId(state: ChatState, conversationId = state.activeConversationId): string | null {
  const entry = Object.entries(state.runs).find(
    ([, run]) =>
      run.conversationId === conversationId && (run.status === 'queued' || run.status === 'running'),
  )
  return entry ? entry[0] : null
}

export function isIncomplete(state: ChatState, message: Message): boolean {
  if (message.role !== 'assistant' || !message.run_id) return false
  const status = state.runs[message.run_id]?.status
  return status === 'failed' || status === 'cancelled'
}
