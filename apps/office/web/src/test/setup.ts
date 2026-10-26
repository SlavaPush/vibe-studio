import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'
import { clearAwaitedDecisions } from '@/lib/use-decision'

afterEach(() => {
  cleanup()
  clearAwaitedDecisions()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
