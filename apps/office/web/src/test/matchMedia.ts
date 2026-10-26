import { vi } from 'vitest'

export function stubSystemTheme(initialDark: boolean) {
  const listeners = new Set<() => void>()
  const query = {
    matches: initialDark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
  }
  vi.stubGlobal('matchMedia', () => query)
  return {
    listenerCount: () => listeners.size,
    setDark(dark: boolean) {
      query.matches = dark
      listeners.forEach((listener) => listener())
    },
  }
}
