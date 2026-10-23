import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { stubSystemTheme } from '@/test/matchMedia'
import { initTheme, THEME_KEY } from '@/theme'
import { ThemeToggle } from './ThemeToggle'

beforeEach(() => {
  window.localStorage.clear()
  stubSystemTheme(false)
  initTheme()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ThemeToggle', () => {
  it('показывает группу с подписью и три варианта, выбрана системная тема', () => {
    render(<ThemeToggle />)
    const group = screen.getByRole('radiogroup', { name: 'Тема оформления' })
    expect(group).toBeInTheDocument()
    const options = screen.getAllByRole('radio')
    expect(options.map((option) => option.getAttribute('aria-label'))).toEqual([
      'Системная тема',
      'Светлая тема',
      'Тёмная тема',
    ])
    expect(screen.getByRole('radio', { name: 'Системная тема' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Тёмная тема' })).toHaveAttribute('aria-checked', 'false')
  })

  it('меняет и сохраняет тему по клику, повторный клик не снимает выбор', () => {
    render(<ThemeToggle />)
    const dark = screen.getByRole('radio', { name: 'Тёмная тема' })
    fireEvent.click(dark)
    expect(dark).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Системная тема' })).toHaveAttribute('aria-checked', 'false')
    expect(document.documentElement).toHaveClass('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')

    fireEvent.click(dark)
    expect(dark).toHaveAttribute('aria-checked', 'true')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')

    fireEvent.click(screen.getByRole('radio', { name: 'Светлая тема' }))
    expect(document.documentElement).not.toHaveClass('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
  })

  it('при ошибке записи хранилища оставляет выбранной системную тему', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    render(<ThemeToggle />)
    fireEvent.click(screen.getByRole('radio', { name: 'Тёмная тема' }))
    expect(screen.getByRole('radio', { name: 'Системная тема' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Тёмная тема' })).toHaveAttribute('aria-checked', 'false')
    expect(document.documentElement).not.toHaveClass('dark')
  })

  it('переключается с клавиатуры: стрелки перемещают фокус, активация выбирает', async () => {
    render(<ThemeToggle />)
    const system = screen.getByRole('radio', { name: 'Системная тема' })
    system.focus()
    fireEvent.keyDown(system, { key: 'ArrowRight' })
    const light = screen.getByRole('radio', { name: 'Светлая тема' })
    await waitFor(() => expect(light).toHaveFocus())
    fireEvent.click(light)
    expect(light).toHaveAttribute('aria-checked', 'true')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
  })
})
