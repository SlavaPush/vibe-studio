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

export interface QueuedMessage {
  id: string
  conversationId: string
  text: string
  model: ModelChoice
  idempotencyKey: string
  createdAt: string
}

export type QueuePauseReason = 'failed' | 'cancelled' | 'check_unavailable' | 'request_failed'

export interface QueuePause {
  reason: QueuePauseReason
  error?: ApiError
}

export interface StoredQueue {
  items: QueuedMessage[]
  pause: QueuePause | null
  trackedRunId: string | null
}

export const QUEUE_LIMIT = 5
export const PENDING_PREFIX = 'pending:'

export interface ChatState {
  session: Session | null
  sessionChecked: boolean
  sessionError: ApiError | null
  sending: boolean
  sendingQueuedId: string | null
  queue: QueuedMessage[]
  queuePause: QueuePause | null
  trackedRunId: string | null
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
export const NO_CONVERSATION = ''

export function initialChatState(): ChatState {
  return {
    session: null,
    sessionChecked: false,
    sessionError: null,
    sending: false,
    sendingQueuedId: null,
    queue: [],
    queuePause: null,
    trackedRunId: null,
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
  const pending = (state.messages[conversationId] ?? []).filter((message) => message.id.startsWith(PENDING_PREFIX))
  return {
    ...state,
    messages: { ...state.messages, [conversationId]: [...list.items, ...pending] },
    loaded: { ...state.loaded, [conversationId]: true },
    runs,
    errors: { ...state.errors, [conversationId]: error },
  }
}

export function pendingMessageId(item: QueuedMessage): string {
  return `${PENDING_PREFIX}${item.idempotencyKey}`
}

export function addRun(state: ChatState, run: Run, userMessage: Message, pendingId?: string): ChatState {
  const messages = state.messages[run.conversation_id] ?? []
  const next = messages.some((message) => message.id === userMessage.id)
    ? upsertMessage(
        messages.filter((message) => message.id !== pendingId),
        userMessage,
      )
    : messages.some((message) => message.id === pendingId)
      ? messages.map((message) => (message.id === pendingId ? userMessage : message))
      : [...messages, userMessage]
  return {
    ...state,
    messages: { ...state.messages, [run.conversation_id]: next },
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
    checkSession() {
      set({ ...state, sessionChecked: false, sessionError: null })
    },
    finishSessionCheck(error: ApiError | null = null) {
      set({ ...state, sessionChecked: true, sessionError: error })
    },
    setSending(sending: boolean, queuedId: string | null = null) {
      set({ ...state, sending, sendingQueuedId: sending ? queuedId : null })
    },
    signIn(session: Session, stored?: StoredQueue) {
      set(
        stored
          ? { ...state, session, queue: stored.items, queuePause: stored.pause, trackedRunId: stored.trackedRunId }
          : { ...state, session },
      )
    },
    enqueue(item: QueuedMessage) {
      set({ ...state, queue: [...state.queue, item] })
    },
    removeQueued(id: string) {
      const queue = state.queue.filter((item) => item.id !== id)
      set({ ...state, queue, queuePause: queue.length > 0 ? state.queuePause : null })
    },
    pauseQueue(pause: QueuePause) {
      set({ ...state, queuePause: pause })
    },
    resumeQueue() {
      set({ ...state, queuePause: null })
    },
    setTrackedRun(runId: string | null) {
      set({ ...state, trackedRunId: runId })
    },
    addPendingMessage(item: QueuedMessage) {
      const message: Message = {
        id: pendingMessageId(item),
        conversation_id: item.conversationId,
        role: 'user',
        kind: null,
        text: item.text,
        run_id: null,
        effective_model: null,
        effort: null,
        asset: null,
        created_at: new Date().toISOString(),
      }
      const messages = state.messages[item.conversationId] ?? []
      set({ ...state, messages: { ...state.messages, [item.conversationId]: upsertMessage(messages, message) } })
    },
    removePendingMessage(item: QueuedMessage) {
      const id = pendingMessageId(item)
      const messages = state.messages[item.conversationId]
      if (!messages?.some((message) => message.id === id)) return
      set({
        ...state,
        messages: { ...state.messages, [item.conversationId]: messages.filter((message) => message.id !== id) },
      })
    },
    signOut() {
      set({
        ...initialChatState(),
        sessionChecked: true,
        models: state.models,
        selectedModel: state.selectedModel,
        updateAvailable: state.updateAvailable,
      })
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
        messages: {
          ...state.messages,
          [conversation.id]: state.messages[NO_CONVERSATION] ?? [],
          [NO_CONVERSATION]: [],
        },
        loaded: { ...state.loaded, [conversation.id]: true },
      })
    },
    loadMessages(conversationId: string, list: MessageList) {
      set(applyMessageList(state, conversationId, list))
    },
    startRun(run: Run, userMessage: Message, pendingId?: string) {
      set(addRun(state, run, userMessage, pendingId))
    },
    setError(conversationId: string, error: ApiError | undefined) {
      set({ ...state, errors: { ...state.errors, [conversationId]: error } })
    },
    showUpdate() {
      set({ ...state, updateAvailable: true })
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

export function hasActiveRun(state: ChatState): boolean {
  return Object.values(state.runs).some((run) => run.status === 'queued' || run.status === 'running')
}

export function isSendBlocked(state: ChatState): boolean {
  const id = state.activeConversationId
  const loading = id !== null && !state.loaded[id] && !state.errors[id]
  return loading || state.queue.length >= QUEUE_LIMIT
}

export function isIncomplete(state: ChatState, message: Message): boolean {
  if (message.role !== 'assistant' || !message.run_id) return false
  const status = state.runs[message.run_id]?.status
  return status === 'failed' || status === 'cancelled'
}
