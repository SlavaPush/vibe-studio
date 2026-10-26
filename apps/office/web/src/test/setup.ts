import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'
import { clearAwaitedDecisions } from '@/lib/use-decision'
import { installEventSource } from '@/test/fake-api'

beforeEach(() => {
  installEventSource()
})

afterEach(() => {
  cleanup()
  clearAwaitedDecisions()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
