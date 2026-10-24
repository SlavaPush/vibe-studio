import type { TaskEvent } from '@/api/types'

export const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000]

export type StreamState = 'connecting' | 'open' | 'reconnecting'

const CLOSED = 2

interface OfficeEventsOptions {
  resync: () => Promise<unknown>
  onEvent: (event: TaskEvent) => void
  onState: (state: StreamState) => void
}

export interface OfficeEvents {
  stop: () => void
  recover: () => void
}

const delayAfter = (failures: number) =>
  RECONNECT_DELAYS_MS[Math.min(failures, RECONNECT_DELAYS_MS.length - 1)]

function listen(source: EventSource, onEvent: (event: TaskEvent) => void) {
  source.addEventListener('task_event', (message) => {
    try {
      onEvent(JSON.parse((message as MessageEvent<string>).data) as TaskEvent)
    } catch {
      return
    }
  })
}

export function connectOfficeEvents({ resync, onEvent, onState }: OfficeEventsOptions): OfficeEvents {
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  let closed = false
  let synced = false
  let dirty = false

  const sync = (current: EventSource) => {
    dirty = false
    resync().then(
      () => {
        if (closed || source !== current) return
        if (dirty) {
          sync(current)
          return
        }
        failures = 0
        synced = true
        onState('open')
      },
      () => {
        if (source === current) retry()
      },
    )
  }

  const retry = () => {
    if (closed) return
    source?.close()
    source = null
    synced = false
    clearTimeout(timer)
    onState('reconnecting')
    const delay = delayAfter(failures)
    failures += 1
    timer = setTimeout(() => void connect(), delay)
  }

  const connect = async () => {
    try {
      await resync()
    } catch {
      retry()
      return
    }
    if (closed) return
    const current = new EventSource('/api/events')
    source = current
    current.addEventListener('open', () => {
      if (closed || source !== current) return
      sync(current)
    })
    listen(current, onEvent)
    current.addEventListener('error', () => {
      if (source === current) retry()
    })
  }

  onState('connecting')
  void connect()

  return {
    stop() {
      closed = true
      clearTimeout(timer)
      source?.close()
      source = null
    },
    recover() {
      if (synced) retry()
      else dirty = true
    },
  }
}

interface TaskEventsOptions {
  onEvent: (event: TaskEvent) => void
  onState: (state: StreamState) => void
  onBroken: () => Promise<unknown>
}

export function openTaskEvents(
  taskId: string,
  { onEvent, onState, onBroken }: TaskEventsOptions,
): () => void {
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  let closed = false

  const open = () => {
    if (closed) return
    const current = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events`)
    source = current
    listen(current, onEvent)
    current.addEventListener('open', () => {
      if (closed || source !== current) return
      failures = 0
      onState('open')
    })
    current.addEventListener('error', () => {
      if (closed || source !== current) return
      onState('reconnecting')
      if (current.readyState !== CLOSED) return
      current.close()
      source = null
      const delay = delayAfter(failures)
      failures += 1
      void onBroken()
        .catch(() => undefined)
        .then(() => {
          if (!closed) timer = setTimeout(open, delay)
        })
    })
  }

  onState('connecting')
  open()

  return () => {
    closed = true
    clearTimeout(timer)
    source?.close()
    source = null
  }
}
