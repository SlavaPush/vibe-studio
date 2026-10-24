import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ABOUT } from '@/about'
import { AboutDialog } from './AboutDialog'

async function open() {
  render(<AboutDialog />)
  const trigger = screen.getByRole('button', { name: 'О приложении' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog', { name: 'О приложении' })
  return { trigger, dialog }
}

describe('AboutDialog', () => {
  it('открывает диалог с описанием, возможностями и технологиями из модуля about', async () => {
    const { trigger, dialog } = await open()
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(dialog).toHaveAccessibleDescription(ABOUT.summary)
    const features = within(dialog).getByRole('heading', { name: 'Что можно делать', level: 3 })
    expect(features).toBeInTheDocument()
    expect(within(dialog).getAllByRole('listitem').map((item) => item.textContent)).toEqual(ABOUT.features)
    expect(within(dialog).getByRole('heading', { name: 'Технологии', level: 3 })).toBeInTheDocument()
    for (const item of ABOUT.technologies) {
      expect(within(dialog).getByText(item.label)).toBeInTheDocument()
      expect(within(dialog).getByText(item.value)).toBeInTheDocument()
    }
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement))
  })

  it('закрывается по Escape и возвращает фокус на кнопку', async () => {
    const { trigger, dialog } = await open()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('закрывается кнопкой «Закрыть» и возвращает фокус на кнопку', async () => {
    const { trigger, dialog } = await open()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Закрыть' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})
