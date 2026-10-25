import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Run } from '@/api/client'
import type { Task } from '@/api/types'
import { TaskForm } from '@/components/office/TaskForm'
import { tasks } from '@/mocks/data'
import { apiError, deferred, fakeApi, json } from '@/test/fake-api'

const direct: Run = (operation) => operation()
const TEXT = 'Добавить кнопку копирования ответа ассистента в буфер обмена.'
const rejectedTask = tasks.find((task) => task.status === 'rejected') as Task
const queuedTask: Task = { ...(tasks.find((task) => task.id === 'tsk_101') as Task), status: 'queued' }

function renderForm() {
  const onCreated = vi.fn()
  render(<TaskForm run={direct} onCreated={onCreated} />)
  const textarea = screen.getByRole('textbox', { name: 'Текст задачи' })
  const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Поставить задачу' }))
  return { onCreated, textarea, submit }
}

describe('TaskForm', () => {
  it('проверяет длину текста до отправки', () => {
    const api = fakeApi()
    const { textarea, submit } = renderForm()
    fireEvent.change(textarea, { target: { value: 'коротко' } })
    expect(textarea).toHaveAccessibleDescription('От 20 до 4000 символов. Введено: 7.')
    submit()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Текст задачи должен содержать от 20 до 4000 символов.',
    )
    expect(textarea).toHaveAttribute('aria-invalid', 'true')
    fireEvent.change(textarea, { target: { value: 'x'.repeat(4001) } })
    submit()
    expect(api.calls).toHaveLength(0)
  })

  it('показывает ожидание фильтра и принятую задачу', async () => {
    const api = fakeApi()
    const response = deferred<Response>()
    api.on('POST /api/tasks', () => response.promise)
    const { textarea, submit, onCreated } = renderForm()
    fireEvent.change(textarea, { target: { value: TEXT } })
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByText(/Профиль/)).not.toBeInTheDocument()
    submit()
    const busy = await screen.findByRole('button', { name: 'Проверяем задачу…' })
    expect(busy).toBeDisabled()
    expect(busy.closest('form')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('status')).toHaveTextContent('Фильтр приёма проверяет задачу')
    expect(api.bodies('POST /api/tasks')).toEqual([{ text: TEXT }])
    response.resolve(json(201, queuedTask))
    expect(await screen.findByText('Задача принята и ждёт свободного слота.')).toBeInTheDocument()
    expect(onCreated).toHaveBeenCalledWith(queuedTask)
    expect(textarea).toHaveValue('')
  })

  it('показывает причину отказа фильтра', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks', () => json(201, rejectedTask))
    const { textarea, submit, onCreated } = renderForm()
    fireEvent.change(textarea, { target: { value: TEXT } })
    submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      `Задача отклонена: ${rejectedTask.rejection_reason}`,
    )
    expect(onCreated).toHaveBeenCalledWith(rejectedTask)
    expect(textarea).toHaveValue(TEXT)
  })

  it('429 показывает паузу из Retry-After и блокирует отправку', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks', () =>
      apiError(429, 'rate_limited', 'Можно поставить не больше пяти задач за час. Повторите позже.', {
        'Retry-After': '30',
      }),
    )
    const { textarea, submit } = renderForm()
    fireEvent.change(textarea, { target: { value: TEXT } })
    submit()
    expect(await screen.findByRole('alert')).toHaveTextContent('Повторить можно через 30 с.')
    expect(screen.getByRole('button', { name: 'Поставить задачу' })).toBeDisabled()
  })

  it('422 показывает текст ошибки сервера', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks', () =>
      apiError(422, 'validation_error', 'Тело запроса не соответствует схеме.'),
    )
    const { textarea, submit } = renderForm()
    fireEvent.change(textarea, { target: { value: TEXT } })
    submit()
    expect(await screen.findByRole('alert')).toHaveTextContent('Тело запроса не соответствует схеме.')
  })
})
