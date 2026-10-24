import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Participant } from '@/api/types'
import App from '@/App'
import { AboutDialog } from '@/components/office/AboutDialog'
import { ABOUT } from '@/lib/about'
import { participants, releaseQueue, releases, tasks } from '@/mocks/data'
import { fakeApi, installEventSource } from '@/test/fake-api'

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
    expect(within(dialog).getByRole('heading', { name: 'Что можно делать', level: 3 })).toBeInTheDocument()
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

  it('кнопка «О приложении» есть в шапке панели после входа', async () => {
    const maria = participants.find((participant) => participant.id === 'prt_maria') as Participant
    installEventSource()
    const api = fakeApi()
    api.on('GET /api/me', maria)
    api.on('GET /api/tasks', tasks)
    api.on('GET /api/releases', releases)
    api.on('GET /api/release-queue', releaseQueue)
    api.on('GET /api/slots', { used: 2, total: 3 })
    render(<App />)
    const heading = await screen.findByRole('heading', { name: 'Панель офиса', level: 1 })
    const header = heading.closest('header') as HTMLElement
    fireEvent.click(within(header).getByRole('button', { name: 'О приложении' }))
    expect(await screen.findByRole('dialog', { name: 'О приложении' })).toBeInTheDocument()
  })
})
