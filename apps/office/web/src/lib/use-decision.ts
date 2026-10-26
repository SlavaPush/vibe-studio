import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { asApiError, isStale, isUnauthorized } from '@/api/client'

export interface DecisionBody<D extends string> {
  decision: D
  expected_version: number
  idempotency_key: string
  fix_sha?: string
  reason?: string
}

export interface DecisionNotice<D extends string> {
  tone: 'info' | 'error'
  text: string
  retry: DecisionBody<D> | null
}

export interface DecisionWait {
  slowText?: string
  settled?: (decisionId: string | null) => boolean
}

export const DECISION_RECORDED = 'Решение записано.'
export const DECISION_SLOW_TASK = 'Решение записано, обновляем задачу…'
export const DECISION_SLOW_MS = 10_000

const SENDING = 'sending'
type Awaited = { version: number; decisionId: string | null } | typeof SENDING

const awaitedDecisions = new Map<string, Awaited>()
const listeners = new Set<() => void>()

function update(key: string, value?: Awaited) {
  if (value === undefined) awaitedDecisions.delete(key)
  else awaitedDecisions.set(key, value)
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function clearAwaitedDecisions() {
  awaitedDecisions.clear()
  listeners.forEach((listener) => listener())
}

export function newDecision<D extends string>(decision: D, version: number): DecisionBody<D> {
  return { decision, expected_version: version, idempotency_key: crypto.randomUUID() }
}

export function useDecision<D extends string>(
  key: string,
  send: (body: DecisionBody<D>) => Promise<{ decision_id?: string } | undefined>,
  refresh: () => void,
  version: number,
  { slowText = DECISION_SLOW_TASK, settled }: DecisionWait = {},
) {
  const awaited = useSyncExternalStore(subscribe, () => awaitedDecisions.get(key))
  const [notice, setNotice] = useState<DecisionNotice<D> | null>(null)
  const latestRefresh = useRef(refresh)
  const sending = awaited === SENDING
  const updating =
    awaited !== undefined && awaited !== SENDING && version <= awaited.version && !settled?.(awaited.decisionId)

  useEffect(() => {
    latestRefresh.current = refresh
  })

  useEffect(() => {
    if (awaited !== undefined && !sending && !updating) update(key)
  }, [key, awaited, sending, updating])

  useEffect(() => {
    if (!updating) return
    const timer = setInterval(() => {
      setNotice({ tone: 'info', text: slowText, retry: null })
      latestRefresh.current()
    }, DECISION_SLOW_MS)
    return () => clearInterval(timer)
  }, [updating, slowText])

  const submit = async (body: DecisionBody<D>) => {
    update(key, SENDING)
    setNotice(null)
    try {
      const result = await send(body)
      setNotice({ tone: 'info', text: DECISION_RECORDED, retry: null })
      update(key, { version: body.expected_version, decisionId: result?.decision_id ?? null })
      refresh()
    } catch (error) {
      update(key)
      const failure = asApiError(error)
      if (isStale(failure) || isUnauthorized(failure)) return
      const retry = failure.code === 'network' ? body : null
      setNotice({ tone: 'error', text: failure.message, retry })
      if (failure.status === 409 || failure.status >= 500) refresh()
    }
  }

  const recorded: DecisionNotice<D> | null = updating ? { tone: 'info', text: DECISION_RECORDED, retry: null } : null
  const current = notice ?? recorded
  const shown = !updating && current?.text === slowText ? { ...current, text: DECISION_RECORDED } : current
  return { pending: sending || updating, notice: shown, submit }
}
