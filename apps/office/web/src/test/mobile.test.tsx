import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Participant, Task } from '@/api/types'
import App from '@/App'
import { participants, releaseQueue, releases, tasks } from '@/mocks/data'
import { fakeApi, installEventSource, json } from '@/test/fake-api'

const maria = participants.find((participant) => participant.id === 'prt_maria') as Participant
const [first, second] = tasks
const NARROW = '(max-width: 1023.98px)'

function backend() {
  installEventSource()
  const api = fakeApi()
  api.on('GET /api/me', maria)
  api.on('GET /api/tasks', tasks)
  api.on('GET /api/releases', releases)
  api.on('GET /api/release-queue', { ...releaseQueue, release_blocked_by: null, blocked_reason: null })
  api.on('GET /api/slots', { used: 2, total: 3 })
  for (const task of tasks) api.on(`GET /api/tasks/${task.id}`, task)
  return api
}

function viewport(matches: boolean) {
  const matchMedia = vi.fn((query: string) => ({ matches: query === NARROW && matches, media: query }))
  vi.stubGlobal('matchMedia', matchMedia)
  return matchMedia
}

const stack = () => screen.getByRole('region', { name: 'Задачи' })
const item = (task: Task) => within(stack()).getByRole('button', { name: new RegExp(task.title) })

async function ready() {
  render(<App />)
  await within(await screen.findByRole('region', { name: 'Задачи' })).findByText(first.title)
  expect(await screen.findByRole('heading', { level: 2, name: first.title })).toBeInTheDocument()
}

function select(task: Task) {
  const button = item(task)
  button.focus()
  fireEvent.click(button)
  return button
}

describe('новые элементы на узкой ширине', () => {
  it('шапка: переключатель темы с целями касания 44 px, список: название в две строки, карточка: описание', async () => {
    backend()
    viewport(true)
    await ready()
    const theme = screen.getByRole('radiogroup', { name: 'Тема оформления' })
    expect(screen.queryByRole('button', { name: 'Тёмная тема' })).not.toBeInTheDocument()
    for (const option of within(theme).getAllByRole('radio')) expect(option).toHaveClass('size-11', 'desk:size-8')
    const title = within(item(first)).getByText(first.title)
    expect(title).toHaveClass('line-clamp-2')
    expect(title).toHaveAttribute('title', first.title)
    expect(item(first)).toHaveTextContent(`Заказчик: ${first.owner_name}`)
    const description = screen.getByRole('region', { name: 'Описание' })
    expect(within(description).getByText(first.text.split('\n')[0], { exact: false })).toHaveClass('whitespace-pre-wrap')
  })
})

describe('строка «Сейчас» на узкой ширине', () => {
  it('переносится без горизонтальной прокрутки', async () => {
    backend()
    viewport(true)
    await ready()
    select(second)
    const status = await screen.findByText('Сейчас:', { exact: false })
    const text = status.closest('[role="status"]') as HTMLElement
    expect(text).toHaveClass('min-w-0', 'break-words')
    expect(text.parentElement).toHaveClass('flex', 'flex-wrap')
    expect(text.parentElement?.className).not.toMatch(/whitespace-nowrap|w-\[|min-w-\[/)
  })
})

describe('выбор задачи на узкой ширине', () => {
  it('переводит фокус на заголовок карточки, в том числе при повторном выборе', async () => {
    backend()
    const matchMedia = viewport(true)
    await ready()
    select(second)
    const title = await screen.findByRole('heading', { level: 2, name: second.title })
    expect(title).toHaveFocus()
    expect(matchMedia).toHaveBeenCalledWith(NARROW)
    select(second)
    expect(screen.getByRole('heading', { level: 2, name: second.title })).toHaveFocus()
  })

  it('на широком экране оставляет фокус на кнопке задачи', async () => {
    backend()
    viewport(false)
    await ready()
    const button = select(second)
    expect(await screen.findByRole('heading', { level: 2, name: second.title })).toBeInTheDocument()
    expect(button).toHaveFocus()
  })

  it('без window.matchMedia оставляет фокус на кнопке задачи', async () => {
    backend()
    vi.stubGlobal('matchMedia', undefined)
    await ready()
    const button = select(second)
    expect(await screen.findByRole('heading', { level: 2, name: second.title })).toBeInTheDocument()
    expect(button).toHaveFocus()
  })

  it.each([true, false])(
    'кнопка задачи в очереди выпуска открывает карточку и переводит фокус на заголовок (узкий экран: %s)',
    async (narrow) => {
      backend()
      viewport(narrow)
      await ready()
      const target = tasks.find(
        (task) => task.id === releases.find((release) => release.status === 'pending')?.task_id,
      ) as Task
      const open = () =>
        within(screen.getByRole('region', { name: 'Очередь выпуска' })).getByRole('button', {
          name: `Открыть задачу «${target.title}»`,
        })
      open().focus()
      fireEvent.click(open())
      expect(await screen.findByRole('heading', { level: 2, name: target.title })).toHaveFocus()
      expect(within(stack()).getByRole('button', { name: new RegExp(target.title) })).toHaveAttribute(
        'aria-current',
        'true',
      )
      open().focus()
      fireEvent.click(open())
      expect(screen.getByRole('heading', { level: 2, name: target.title })).toHaveFocus()
    },
  )

  it('создание задачи через форму не переводит фокус на карточку', async () => {
    const api = backend()
    viewport(true)
    const created: Task = { ...first, id: 'tsk_300', title: 'Задача из формы на телефоне', status: 'queued', version: 1 }
    api.on('POST /api/tasks', () => json(201, created))
    await ready()
    const text = screen.getByRole('textbox', { name: 'Текст задачи' })
    fireEvent.change(text, { target: { value: 'Задача из формы на телефоне для vibe-chat.' } })
    const submit = screen.getByRole('button', { name: 'Поставить задачу' })
    submit.focus()
    fireEvent.click(submit)
    const title = await screen.findByRole('heading', { level: 2, name: created.title })
    expect(title).not.toHaveFocus()
    expect(within(stack()).getByRole('button', { name: new RegExp(created.title) })).toHaveAttribute(
      'aria-current',
      'true',
    )
  })
})
