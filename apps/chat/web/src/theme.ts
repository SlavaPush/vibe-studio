import { useSyncExternalStore } from 'react'

export type ThemeChoice = 'system' | 'light' | 'dark'

export const THEME_KEY = 'vibe-chat-theme'
const CHOICES: ThemeChoice[] = ['system', 'light', 'dark']

let current: ThemeChoice = 'system'
let systemQuery: MediaQueryList | null = null
const listeners = new Set<() => void>()

export function readTheme(): ThemeChoice {
  try {
    const value = window.localStorage.getItem(THEME_KEY)
    return CHOICES.find((choice) => choice === value) ?? 'system'
  } catch {
    return 'system'
  }
}

function applyTheme() {
  const dark = current === 'system' ? Boolean(systemQuery?.matches) : current === 'dark'
  document.documentElement.classList.toggle('dark', dark)
}

function selectChoice(choice: ThemeChoice) {
  current = choice
  systemQuery?.removeEventListener('change', applyTheme)
  systemQuery = null
  if (choice === 'system') {
    systemQuery = window.matchMedia('(prefers-color-scheme: dark)')
    systemQuery.addEventListener('change', applyTheme)
  }
  applyTheme()
  listeners.forEach((listener) => listener())
}

function saveTheme(choice: ThemeChoice): boolean {
  try {
    window.localStorage.setItem(THEME_KEY, choice)
    return true
  } catch {
    return false
  }
}

export function initTheme() {
  selectChoice(readTheme())
}

export function setTheme(choice: ThemeChoice) {
  selectChoice(saveTheme(choice) ? choice : 'system')
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useTheme(): ThemeChoice {
  return useSyncExternalStore(subscribe, () => current)
}
