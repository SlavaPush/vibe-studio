import { ApiRequestError, isNetworkError, type ApiClient } from '@/api/client'
import type { Message, MessageList, Run, RunEvent, RunEventType } from '@/api/types'
import { activeRunId, type ChatStore } from './conversations'

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

export function createChatController(
  store: ChatStore,
  api: ApiClient,
  openEvents: EventSourceFactory = (url) => new EventSource(url),
) {
  const sources = new Map<string, EventSource>()
  const loading = new Map<string, Promise<void>>()
  let generation = 0

  function close(runId: string) {
    sources.get(runId)?.close()
    sources.delete(runId)
  }

  function signOut() {
    generation += 1
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
    if (conversationId) store.setError(conversationId, error.error)
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
      if (TERMINAL_EVENTS.has(event.type)) close(runId)
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
      if (run.status === 'queued' || run.status === 'running') {
        subscribe(runId)
        return
      }
      applyHistory(run.conversation_id, await api.messages(run.conversation_id))
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
    try {
      const list = await api.conversations()
      store.setConversations(list.items)
      const id = store.getState().activeConversationId ?? list.items[0]?.id
      if (id) {
        store.selectConversation(id)
        await loadConversation(id)
      }
    } catch (error) {
      handleError(error)
    }
  }

  return {
    async start() {
      api
        .models()
        .then((catalog) => store.setModels(catalog.items))
        .catch(() => {})
      try {
        store.signIn(await api.me())
      } catch (error) {
        if (error instanceof ApiRequestError && error.status === 401) return
        handleError(error)
        return
      }
      await loadConversations()
    },

    async signIn(code: string) {
      store.signIn(await api.createSession(code))
      await loadConversations()
    },

    async selectConversation(id: string) {
      store.selectConversation(id)
      await loadConversation(id)
    },

    async createConversation() {
      try {
        store.addConversation(await api.createConversation())
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

    async send(text: string) {
      let conversationId = store.getState().activeConversationId
      try {
        if (!conversationId) {
          const conversation = await api.createConversation()
          store.addConversation(conversation)
          conversationId = conversation.id
        }
        await loadConversation(conversationId)
        if (!store.getState().loaded[conversationId]) return
        const body = {
          text,
          model: store.getState().selectedModel,
          idempotency_key: crypto.randomUUID(),
        }
        let run: Run
        try {
          run = await api.createRun(conversationId, body)
        } catch (error) {
          if (!isNetworkError(error)) throw error
          run = await api.createRun(conversationId, body)
        }
        store.startRun(run, userMessage(run, text))
        subscribe(run.id)
        api
          .conversations()
          .then((list) => store.setConversations(list.items))
          .catch(() => {})
      } catch (error) {
        handleError(error, conversationId)
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

    loadAssetUrl: (id: string) => api.assetUrl(id),
  }
}

export type ChatController = ReturnType<typeof createChatController>
