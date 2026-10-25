import { useState } from 'react'
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

export const DECISION_RECORDED = 'Решение записано.'

export function newDecision<D extends string>(decision: D, version: number): DecisionBody<D> {
  return { decision, expected_version: version, idempotency_key: crypto.randomUUID() }
}

export function useDecision<D extends string>(
  send: (body: DecisionBody<D>) => Promise<unknown>,
  refresh: () => void,
) {
  const [pending, setPending] = useState(false)
  const [notice, setNotice] = useState<DecisionNotice<D> | null>(null)

  const submit = async (body: DecisionBody<D>) => {
    setPending(true)
    setNotice(null)
    try {
      await send(body)
      setNotice({ tone: 'info', text: DECISION_RECORDED, retry: null })
      refresh()
    } catch (error) {
      const failure = asApiError(error)
      if (isStale(failure) || isUnauthorized(failure)) return
      const retry = failure.code === 'network' ? body : null
      setNotice({ tone: 'error', text: failure.message, retry })
      if (failure.status === 409 || failure.status >= 500) refresh()
    } finally {
      setPending(false)
    }
  }

  return { pending, notice, submit }
}
