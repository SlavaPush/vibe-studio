import { ApiRequestError, isNetworkError, type ApiClient } from '@/api/client'
import type { Message, MessageList, Run, RunEvent, RunEventType, RunStatus, Session } from '@/api/types'
import type { SendResult } from './assistantAdapter'
import {
  activeRunId,
  hasActiveRun,
  NO_CONVERSATION,
  pendingMessageId,
  QUEUE_LIMIT,
  type ChatState,
  type ChatStore,
  type QueuedMessage,
  type QueuePause,
} from './conversations'
import { activeConversationKey, readLocal, writeLocal } from './drafts'
import { readQueue, writeQueue } from './queueStorage'

const EVENT_TYPES: RunEventType[] = [
  'accepted',
  'checks_passed',
  'refusal',
  'check_unavailable',
  'model_selected',
  'model_switched',
  'text_delta',
  'asset_ready',
  'completed',
  'failed',
]
const TERMINAL_EVENTS = new Set<RunEventType>(['completed', 'refusal', 'check_unavailable', 'failed'])
const RESUME_DELAYS = [1000, 2000, 5000, 10000, 15000]
const PAUSE_STATUSES = new Set<RunStatus>(['failed', 'cancelled', 'check_unavailable'])

export type EventSourceFactory = (url: string) => EventSource

function userMessage(run: Run, text: string): Message {
  return {
    id: run.user_message_id,
    conversation_id: run.conversation_id,
    role: 'user',
    kind: null,
    text,
    run_id: run.id,
    effective_model: null,
    effort: null,
    asset: null,
    created_at: run.created_at,
  }
}

function isActive(status: RunStatus | undefined): boolean {
  return status === 'queued' || status === 'running'
}

export function createChatController(
  store: ChatStore,
  api: ApiClient,
  openEvents: EventSourceFactory = (url) => new EventSource(url),
) {
  const sources = new Map<string, EventSource>()
  const loading = new Map<string, Promise<void>>()
  let generation = 0
  let restoring = false
  let creating: Promise<string | null> | null = null
  let persisted: Pick<ChatState, 'queue' | 'queuePause' | 'trackedRunId'> | null = null

  store.subscribe(() => {
    const { session, queue, queuePause, trackedRunId } = store.getState()
    if (!session) {
      persisted = null
      return
    }
    if (
      persisted &&
      persisted.queue === queue &&
      persisted.queuePause === queuePause &&
      persisted.trackedRunId === trackedRunId
    ) {
      return
    }
    persisted = { queue, queuePause, trackedRunId }
    writeQueue(session.user_id, { items: queue, pause: queuePause, trackedRunId })
  })

  function close(runId: string) {
    sources.get(runId)?.close()
    sources.delete(runId)
  }

  function rememberActive(id: string) {
    const userId = store.getState().session?.user_id
    if (userId) writeLocal(activeConversationKey(userId), id)
  }

  function signIn(session: Session) {
    restoring = true
    store.signIn(session, readQueue(session.user_id))
  }

  function signOut() {
    generation += 1
    restoring = false
    creating = null
    for (const runId of [...sources.keys()]) close(runId)
    loading.clear()
    api.forgetSession()
    store.signOut()
  }

  function handleError(error: unknown, conversationId?: string | null) {
    if (!(error instanceof ApiRequestError)) throw error
    if (error.status === 401) {
      signOut()
      return
    }
    if (conversationId !== undefined) store.setError(conversationId ?? NO_CONVERSATION, error.error)
  }

  function pauseQueue(pause: QueuePause) {
    if (store.getState().queue.length > 0) store.pauseQueue(pause)
  }

  function finishRun(runId: string, status: RunStatus) {
    if (store.getState().trackedRunId === runId) store.setTrackedRun(null)
    if (PAUSE_STATUSES.has(status)) pauseQueue({ reason: status as QueuePause['reason'] })
    pump()
  }

  function subscribe(runId: string) {
    if (sources.has(runId)) return
    const lastSeq = store.getState().runs[runId]?.lastSeq ?? 0
    const query = lastSeq > 0 ? `?after_seq=${lastSeq}` : ''
    const source = openEvents(`/api/runs/${runId}/events${query}`)
    sources.set(runId, source)
    const onEvent = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as RunEvent
      store.dispatch(event)
      if (!TERMINAL_EVENTS.has(event.type)) return
      close(runId)
      const status = store.getState().runs[runId]?.status
      if (status && !isActive(status)) finishRun(runId, status)
    }
    for (const type of EVENT_TYPES) source.addEventListener(type, onEvent as EventListener)
    source.onerror = () => {
      if (source.readyState !== EventSource.CLOSED || sources.get(runId) !== source) return
      close(runId)
      void resume(runId, generation, 0)
    }
  }

  async function resume(runId: string, session: number, attempt: number) {
    await new Promise((resolve) => setTimeout(resolve, RESUME_DELAYS[Math.min(attempt, RESUME_DELAYS.length - 1)]))
    if (session !== generation || sources.has(runId)) return
    try {
      const run = await api.run(runId)
      if (isActive(run.status)) {
        subscribe(runId)
        return
      }
      applyHistory(run.conversation_id, await api.messages(run.conversation_id))
      if (session === generation) finishRun(runId, run.status)
    } catch (error) {
      if (!(error instanceof ApiRequestError)) throw error
      if (error.status === 401) signOut()
      else if (error.status === 0 || error.status >= 500) void resume(runId, session, attempt + 1)
    }
  }

  function applyHistory(id: string, list: MessageList) {
    store.loadMessages(id, list)
    if (list.active_run_id) {
      close(list.active_run_id)
      subscribe(list.active_run_id)
    }
  }

  async function fetchConversation(id: string) {
    try {
      const list = await api.messages(id)
      if (!store.getState().loaded[id]) applyHistory(id, list)
    } catch (error) {
      handleError(error, id)
    }
  }

  function loadConversation(id: string): Promise<void> {
    if (store.getState().loaded[id]) return Promise.resolve()
    let pending = loading.get(id)
    if (!pending) {
      pending = fetchConversation(id).finally(() => loading.delete(id))
      loading.set(id, pending)
    }
    return pending
  }

  async function loadConversations() {
    let id: string | undefined
    try {
      const list = await api.conversations()
      store.setConversations(list.items)
      const { activeConversationId, session } = store.getState()
      const saved = session ? readLocal(activeConversationKey(session.user_id)) : ''
      id = activeConversationId ?? list.items.find((item) => item.id === saved)?.id ?? list.items[0]?.id
      if (id) store.selectConversation(id)
    } catch (error) {
      handleError(error)
    } finally {
      if (!store.getState().sessionChecked) store.finishSessionCheck()
    }
    if (id) await loadConversation(id)
  }

  async function restoreTrackedRun(session: number) {
    const runId = store.getState().trackedRunId
    if (!runId) return
    let run: Run
    try {
      run = await api.run(runId)
    } catch (error) {
      if (!(error instanceof ApiRequestError)) throw error
      if (error.status === 401) signOut()
      else if (session === generation) pauseQueue({ reason: 'request_failed', error: error.error })
      return
    }
    if (session !== generation) return
    if (isActive(run.status)) {
      await loadConversation(run.conversation_id)
      if (session !== generation) return
      const known = store.getState().runs[runId]?.status
      if (isActive(known)) {
        subscribe(runId)
        return
      }
      if (!known) {
        store.setTrackedRun(null)
        pauseQueue({ reason: 'request_failed', error: store.getState().errors[run.conversation_id] })
        return
      }
      finishRun(runId, known)
      return
    }
    finishRun(runId, run.status)
  }

  async function restoreQueue() {
    const session = generation
    await loadConversations()
    if (session !== generation) return
    await restoreTrackedRun(session)
    if (session !== generation) return
    restoring = false
    pump()
  }

  async function createRunFor(item: QueuedMessage): Promise<Run | null> {
    const session = generation
    await loadConversation(item.conversationId)
    if (session !== generation) return null
    if (!store.getState().loaded[item.conversationId] || hasActiveRun(store.getState())) return null
    store.addPendingMessage(item)
    const body = { text: item.text, model: item.model, idempotency_key: item.idempotencyKey }
    let run: Run
    try {
      try {
        run = await api.createRun(item.conversationId, body)
      } catch (error) {
        if (!isNetworkError(error)) throw error
        run = await api.createRun(item.conversationId, body)
      }
    } catch (error) {
      if (session === generation) store.removePendingMessage(item)
      throw error
    }
    if (session !== generation) return null
    const known = store.getState().runs[run.id]?.status
    store.startRun(run, userMessage(run, item.text), pendingMessageId(item))
    if (known && !isActive(known)) {
      if (PAUSE_STATUSES.has(known)) pauseQueue({ reason: known as QueuePause['reason'] })
    } else {
      store.setTrackedRun(run.id)
      subscribe(run.id)
    }
    api
      .conversations()
      .then((list) => store.setConversations(list.items))
      .catch(() => {})
    return run
  }

  function newItem(conversationId: string, text: string): QueuedMessage {
    return {
      id: crypto.randomUUID(),
      conversationId,
      text,
      model: store.getState().selectedModel,
      idempotencyKey: crypto.randomUUID(),
      createdAt: new Date().toISOString(),
    }
  }

  function pump() {
    const state = store.getState()
    const head = state.queue[0]
    if (!head || restoring || !state.session || state.queuePause || isRunBusy(state)) return
    const session = generation
    store.setSending(true, head.id)
    void createRunFor(head)
      .then((run) => {
        if (session !== generation) return
        if (run) {
          store.removeQueued(head.id)
          return
        }
        if (!store.getState().loaded[head.conversationId]) {
          pauseQueue({ reason: 'request_failed', error: store.getState().errors[head.conversationId] })
        }
      })
      .catch((error: unknown) => {
        if (!(error instanceof ApiRequestError)) throw error
        if (error.status === 401) signOut()
        else if (session === generation) pauseQueue({ reason: 'request_failed', error: error.error })
      })
      .finally(() => {
        if (session !== generation) return
        store.setSending(false)
        pump()
      })
  }

  function isRunBusy(state: ChatState): boolean {
    return state.sending || hasActiveRun(state) || state.trackedRunId !== null
  }

  function ensureConversation(): Promise<string | null> {
    const id = store.getState().activeConversationId
    if (id) return Promise.resolve(id)
    if (!creating) {
      const session = generation
      const pending: Promise<string | null> = api
        .createConversation()
        .then((conversation) => {
          if (session !== generation) return null
          store.addConversation(conversation)
          rememberActive(conversation.id)
          return conversation.id
        })
        .finally(() => {
          if (creating === pending) creating = null
        })
      creating = pending
    }
    return creating
  }

  async function enqueue(text: string): Promise<SendResult> {
    const activeId = store.getState().activeConversationId
    if (store.getState().queue.length >= QUEUE_LIMIT) return { sent: false, conversationId: activeId }
    const item = newItem(activeId ?? NO_CONVERSATION, text)
    const session = generation
    let conversationId: string | null
    try {
      conversationId = await ensureConversation()
    } catch (error) {
      handleError(error, activeId)
      return { sent: false, conversationId: activeId }
    }
    if (session !== generation || !conversationId) return { sent: false, conversationId: activeId }
    if (store.getState().queue.length >= QUEUE_LIMIT) return { sent: false, conversationId }
    store.enqueue({ ...item, conversationId })
    pump()
    return { sent: true, conversationId }
  }

  function resumeQueue() {
    store.resumeQueue()
    const runId = store.getState().trackedRunId
    if (!runId || restoring || sources.has(runId)) {
      pump()
      return
    }
    const session = generation
    restoring = true
    void restoreTrackedRun(session).finally(() => {
      if (session !== generation) return
      restoring = false
      pump()
    })
  }

  return {
    async start() {
      api
        .models()
        .then((catalog) => store.setModels(catalog.items))
        .catch(() => {})
      store.checkSession()
      try {
        signIn(await api.me())
      } catch (error) {
        if (!(error instanceof ApiRequestError)) throw error
        if (error.status === 401) signOut()
        else store.finishSessionCheck(error.error)
        return
      }
      await restoreQueue()
    },

    async signIn(code: string) {
      signIn(await api.createSession(code))
      await restoreQueue()
    },

    async selectConversation(id: string) {
      store.selectConversation(id)
      rememberActive(id)
      await loadConversation(id)
    },

    async createConversation() {
      try {
        const conversation = await api.createConversation()
        store.addConversation(conversation)
        rememberActive(conversation.id)
      } catch (error) {
        handleError(error)
      }
    },

    async renameConversation(id: string, title: string) {
      try {
        store.upsertConversation(await api.renameConversation(id, title))
      } catch (error) {
        handleError(error, id)
      }
    },

    async send(text: string): Promise<SendResult> {
      const state = store.getState()
      if (restoring || isRunBusy(state) || state.queue.length > 0) return enqueue(text)
      let conversationId = state.activeConversationId
      const item = newItem(conversationId ?? NO_CONVERSATION, text)
      const session = generation
      store.setSending(true)
      try {
        if (!conversationId) {
          store.addPendingMessage(item)
          try {
            conversationId = await ensureConversation()
          } finally {
            if (!conversationId && session === generation) store.removePendingMessage(item)
          }
          if (!conversationId) return { sent: false, conversationId }
        }
        const run = await createRunFor({ ...item, conversationId })
        return { sent: run !== null, conversationId }
      } catch (error) {
        if (error instanceof ApiRequestError && error.status !== 401) {
          pauseQueue({ reason: 'request_failed', error: error.error })
        }
        handleError(error, conversationId)
        return { sent: false, conversationId }
      } finally {
        store.setSending(false)
        pump()
      }
    },

    async cancel() {
      const state = store.getState()
      const runId = activeRunId(state)
      if (!runId) return
      try {
        await api.cancelRun(runId)
      } catch (error) {
        handleError(error, state.activeConversationId)
      }
    },

    removeQueued(id: string) {
      store.removeQueued(id)
    },

    resumeQueue,

    loadAssetUrl: (id: string) => api.assetUrl(id),
  }
}

export type ChatController = ReturnType<typeof createChatController>
