import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { stubSystemTheme } from '@/test/matchMedia'
import themeInitScript from '../public/theme-init.js?raw'
import { initTheme, readTheme, setTheme, THEME_KEY } from './theme'

const isDark = () => document.documentElement.classList.contains('dark')
const runThemeInit = () => new Function(themeInitScript)()

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('theme', () => {
  it('по умолчанию следует системной теме и реагирует на её смену без перезагрузки', () => {
    const system = stubSystemTheme(true)
    initTheme()
    expect(readTheme()).toBe('system')
    expect(isDark()).toBe(true)

    system.setDark(false)
    expect(isDark()).toBe(false)
    system.setDark(true)
    expect(isDark()).toBe(true)
  })

  it('сохраняет выбранную тему и применяет её после перезагрузки', () => {
    stubSystemTheme(false)
    initTheme()
    setTheme('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(isDark()).toBe(true)

    document.documentElement.classList.remove('dark')
    initTheme()
    expect(readTheme()).toBe('dark')
    expect(isDark()).toBe(true)
  })

  it('в режимах «Светлая» и «Тёмная» не реагирует на смену системной темы', () => {
    const system = stubSystemTheme(false)
    initTheme()
    expect(system.listenerCount()).toBe(1)

    setTheme('dark')
    expect(system.listenerCount()).toBe(0)
    system.setDark(false)
    expect(isDark()).toBe(true)

    setTheme('light')
    system.setDark(true)
    expect(isDark()).toBe(false)

    setTheme('system')
    expect(system.listenerCount()).toBe(1)
    expect(isDark()).toBe(true)
  })

  it('при неизвестном значении в хранилище использует системную тему', () => {
    stubSystemTheme(true)
    window.localStorage.setItem(THEME_KEY, 'sepia')
    initTheme()
    expect(readTheme()).toBe('system')
    expect(isDark()).toBe(true)
  })

  it('при ошибке чтения хранилища использует системную тему и не прерывает работу', () => {
    stubSystemTheme(true)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => initTheme()).not.toThrow()
    expect(readTheme()).toBe('system')
    expect(isDark()).toBe(true)
  })

  it('при ошибке записи хранилища остаётся на системной теме и следует за ОС', () => {
    const system = stubSystemTheme(true)
    initTheme()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })

    expect(() => setTheme('light')).not.toThrow()
    expect(isDark()).toBe(true)
    expect(system.listenerCount()).toBe(1)

    system.setDark(false)
    expect(isDark()).toBe(false)
  })
})

describe('theme-init.js', () => {
  it.each([
    { stored: null, systemDark: true, dark: true },
    { stored: null, systemDark: false, dark: false },
    { stored: 'system', systemDark: true, dark: true },
    { stored: 'light', systemDark: true, dark: false },
    { stored: 'dark', systemDark: false, dark: true },
    { stored: 'sepia', systemDark: true, dark: true },
  ])('сохранено $stored, системная тёмная $systemDark → тёмная $dark', ({ stored, systemDark, dark }) => {
    stubSystemTheme(systemDark)
    if (stored) window.localStorage.setItem(THEME_KEY, stored)
    runThemeInit()
    expect(isDark()).toBe(dark)
  })

  it('при ошибке хранилища применяет системную тему', () => {
    stubSystemTheme(true)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => runThemeInit()).not.toThrow()
    expect(isDark()).toBe(true)
  })
})
