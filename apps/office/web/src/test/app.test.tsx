import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { NETWORK_MESSAGE } from '@/api/client'
import type { Participant, Task } from '@/api/types'
import App from '@/App'
import { COOKIE_MISSING } from '@/components/office/AccessGate'
import { participants, releases, tasks } from '@/mocks/data'
import {
  FakeEventSource,
  apiError,
  deferred,
  fakeApi,
  installEventSource,
  json,
  taskEvent,
  unauthorized,
} from '@/test/fake-api'

const maria = participants.find((participant) => participant.id === 'prt_maria') as Participant
const developing = tasks.find((task) => task.id === 'tsk_101') as Task
const STREAM = '/api/events'
const SLOW = { timeout: 3000 }

function backend(participant: Participant | null = maria) {
  installEventSource()
  const api = fakeApi()
  api.on('GET /api/me', () => (participant ? json(200, participant) : unauthorized()))
  api.on('GET /api/tasks', tasks)
  api.on('GET /api/releases', releases)
  api.on('GET /api/slots', { used: 2, total: 3 })
  for (const task of tasks) api.on(`GET /api/tasks/${task.id}`, task)
  return api
}

const stack = () => screen.getByRole('region', { name: 'Задачи' })

async function ready() {
  await within(await screen.findByRole('region', { name: 'Задачи' })).findByText(developing.title)
}

async function login(code = 'OFFICE-2026', name = 'Мария') {
  fireEvent.change(await screen.findByLabelText('Код доступа'), { target: { value: code } })
  fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: name } })
  fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
}

function openStream() {
  const source = FakeEventSource.last(STREAM)
  act(() => source.open())
  return source
}

describe('вход', () => {
  it('без сеанса показывает экран входа и не загружает данные', async () => {
    const api = backend(null)
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Вход в панель офиса' })).toBeInTheDocument()
    expect(api.count('GET /api/tasks')).toBe(0)
    expect(screen.queryByText(/OFFICE-2026/)).not.toBeInTheDocument()
  })

  it('вход создаёт сеанс, берёт участника из GET /api/me и загружает данные из API', async () => {
    const api = backend(null)
    let session = false
    api.on('GET /api/me', () => (session ? json(200, maria) : unauthorized()))
    api.on('POST /api/office-session', () => {
      session = true
      return json(200, maria)
    })
    render(<App />)
    await login()
    await ready()
    const paths = api.calls.map((call) => `${call.method} ${call.path}`)
    expect(paths.slice(0, 3)).toEqual(['GET /api/me', 'POST /api/office-session', 'GET /api/me'])
    expect(api.bodies('POST /api/office-session')).toEqual([
      { access_code: 'OFFICE-2026', display_name: 'Мария' },
    ])
    expect(screen.getByText('Мария (участник)')).toBeInTheDocument()
    expect(await within(stack()).findByText('Занято слотов: 2 из 3')).toBeInTheDocument()
    expect(screen.getByText(releases[0].id)).toBeInTheDocument()
    expect(screen.queryByText('Тестовые данные')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Тестовый участник')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Выйти' })).toBeEnabled()
  })

  it('после перезагрузки страницы участник определяется по GET /api/me', async () => {
    const api = backend()
    render(<App />)
    await ready()
    expect(api.count('POST /api/office-session')).toBe(0)
    expect(screen.getByText('Мария (участник)')).toBeInTheDocument()
  })

  it('401 от GET /api/me после входа — сеанс не создан', async () => {
    const api = backend(null)
    api.on('POST /api/office-session', maria)
    render(<App />)
    await login()
    expect(await screen.findByRole('alert')).toHaveTextContent(COOKIE_MISSING)
    expect(api.count('GET /api/tasks')).toBe(0)
  })

  it.each([
    [apiError(401, 'invalid_access_code', 'Неверный код доступа офиса.'), 'Неверный код доступа офиса.'],
    [
      apiError(429, 'rate_limited', 'Слишком много попыток входа. Повторите через минуту.', {
        'Retry-After': '42',
      }),
      'Слишком много попыток входа. Повторите через минуту. Повторить можно через 42 с.',
    ],
    [apiError(422, 'validation_error', 'Укажите отображаемое имя.'), 'Укажите отображаемое имя.'],
  ])('ошибка входа показывается понятным текстом (%#)', async (response, text) => {
    const api = backend(null)
    api.on('POST /api/office-session', () => response.clone())
    render(<App />)
    await login()
    expect(await screen.findByRole('alert')).toHaveTextContent(text)
    if (response.status === 429) expect(screen.getByRole('button', { name: 'Войти' })).toBeDisabled()
  })

  it('401 во время работы показывает вход, закрывает потоки и не применяет ответы прежнего сеанса', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    const late = deferred<Response>()
    api.on('GET /api/tasks/tsk_101', () => late.promise)
    act(() => source.emit(taskEvent('tsk_101', 'role_started', { step: 'implement' })))
    await waitFor(() => expect(api.count('GET /api/tasks/tsk_101')).toBe(1))

    api.on('POST /api/tasks', unauthorized)
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст задачи' }), {
      target: { value: 'Добавить кнопку копирования ответа ассистента.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Поставить задачу' }))
    expect(await screen.findByRole('heading', { name: 'Вход в панель офиса' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Сеанс истёк')
    expect(FakeEventSource.instances.every((item) => item.closed)).toBe(true)

    api.on('POST /api/office-session', maria)
    api.on('GET /api/tasks/tsk_101', developing)
    await login()
    await ready()
    late.resolve(json(200, { ...developing, title: 'Ответ прежнего сеанса', version: 99 }))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByText('Ответ прежнего сеанса')).not.toBeInTheDocument()
  })
})

describe('выход', () => {
  const admin = participants.find((participant) => participant.id === 'prt_admin') as Participant
  const noContent = () => new Response(null, { status: 204 })

  it('«Выйти» удаляет сеанс, закрывает потоки, очищает данные и позволяет войти под другой ролью', async () => {
    const api = backend()
    let current: Participant | null = maria
    api.on('GET /api/me', () => (current ? json(200, current) : unauthorized()))
    api.on('DELETE /api/office-session', () => {
      current = null
      return noContent()
    })
    render(<App />)
    await ready()
    const source = openStream()
    fireEvent.click(within(stack()).getByRole('button', { name: new RegExp(developing.title) }))
    fireEvent.mouseDown(await screen.findByRole('tab', { name: 'Ход работы' }), { button: 0 })
    await waitFor(() => expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(1))
    const late = deferred<Response>()
    api.on('GET /api/tasks/tsk_101', () => late.promise)
    act(() => source.emit(taskEvent('tsk_101', 'role_started', { step: 'implement' })))
    await waitFor(() => expect(api.count('GET /api/tasks/tsk_101')).toBe(1))

    const button = screen.getByRole('button', { name: 'Выйти' })
    button.focus()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(await screen.findByRole('heading', { name: 'Вход в панель офиса' })).toBeInTheDocument()
    expect(api.count('DELETE /api/office-session')).toBe(1)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(FakeEventSource.instances.every((item) => item.closed)).toBe(true)

    api.on('POST /api/office-session', () => {
      current = admin
      return json(200, admin)
    })
    api.on('GET /api/tasks/tsk_101', developing)
    await login('ADMIN-2026', admin.display_name)
    await ready()
    expect(screen.getByText(`${admin.display_name} (администратор)`)).toBeInTheDocument()
    expect(api.bodies('POST /api/office-session')).toEqual([
      { access_code: 'ADMIN-2026', display_name: admin.display_name },
    ])
    expect(screen.queryByRole('heading', { name: developing.title })).not.toBeInTheDocument()
    late.resolve(json(200, { ...developing, title: 'Ответ прежнего сеанса', version: 99 }))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByText('Ответ прежнего сеанса')).not.toBeInTheDocument()
  })

  it('сетевая ошибка выхода показывается, панель и потоки остаются', async () => {
    const api = backend()
    api.on('DELETE /api/office-session', () => Promise.reject(new TypeError('Failed to fetch')))
    render(<App />)
    await ready()
    const source = openStream()
    fireEvent.click(screen.getByRole('button', { name: 'Выйти' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(NETWORK_MESSAGE)
    expect(screen.getByRole('button', { name: 'Выйти' })).toBeEnabled()
    expect(source.closed).toBe(false)
    expect(within(stack()).getByText(developing.title)).toBeInTheDocument()
    expect(screen.getByText('Мария (участник)')).toBeInTheDocument()

    api.on('DELETE /api/office-session', noContent)
    fireEvent.click(screen.getByRole('button', { name: 'Выйти' }))
    expect(await screen.findByRole('heading', { name: 'Вход в панель офиса' })).toBeInTheDocument()
    expect(source.closed).toBe(true)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(api.count('DELETE /api/office-session')).toBe(2)
  })
})

describe('поток задачи', () => {
  it('истёкший сеанс при открытии журнала возвращает экран входа', async () => {
    const api = backend()
    render(<App />)
    await ready()
    openStream()
    api.on('GET /api/me', unauthorized)
    fireEvent.click(within(stack()).getByRole('button', { name: new RegExp(developing.title) }))
    fireEvent.mouseDown(await screen.findByRole('tab', { name: 'Ход работы' }), { button: 0 })
    act(() => FakeEventSource.last('/api/tasks/tsk_101/events').fail('closed'))
    expect(await screen.findByRole('heading', { name: 'Вход в панель офиса' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Сеанс истёк')
    expect(FakeEventSource.instances.every((item) => item.closed)).toBe(true)
  })
})

describe('обновление через SSE', () => {
  it('status_changed перечитывает задачу и слоты, события объединяются', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    api.on('GET /api/tasks/tsk_101', { ...developing, status: 'reviewing', version: 6 })
    api.on('GET /api/slots', { used: 3, total: 3 })
    const before = api.count('GET /api/slots')
    act(() => {
      source.emit(taskEvent('tsk_101', 'status_changed', { from: 'developing', to: 'reviewing' }))
      source.emit(taskEvent('tsk_101', 'role_started', { step: 'qa' }))
    })
    expect(await within(stack()).findByText('Проверка')).toBeInTheDocument()
    expect(await within(stack()).findByText('Занято слотов: 3 из 3')).toBeInTheDocument()
    expect(api.count('GET /api/tasks/tsk_101')).toBe(1)
    expect(api.count('GET /api/slots')).toBe(before + 1)
  })

  it('событие выпуска перечитывает очередь выпусков', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    const before = api.count('GET /api/releases')
    api.on('GET /api/releases', [{ ...releases[0], id: 'rel_8', version: 1 }, ...releases])
    act(() => source.emit(taskEvent('tsk_102', 'release_candidate', { release_id: 'rel_8' })))
    expect(await screen.findByText('rel_8')).toBeInTheDocument()
    expect(api.count('GET /api/releases')).toBe(before + 1)
  })

  it('после разрыва перечитывает списки до подключения и повторно после open', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    expect(api.count('GET /api/tasks')).toBe(2)
    act(() => source.fail())
    expect(source.closed).toBe(true)
    expect(screen.getByText('Переподключение к обновлениям…')).toHaveAttribute('role', 'status')
    await waitFor(() => expect(FakeEventSource.all(STREAM)).toHaveLength(2), SLOW)
    expect(api.count('GET /api/tasks')).toBe(3)
    expect(api.count('GET /api/releases')).toBe(3)
    expect(api.count('GET /api/slots')).toBe(3)

    api.on('GET /api/tasks', tasks.map((task) =>
      task.id === 'tsk_101' ? { ...task, status: 'awaiting_integration', version: 9 } : task,
    ))
    openStream()
    expect(await within(stack()).findByText('Ждёт слияния PR')).toBeInTheDocument()
    expect(api.count('GET /api/tasks')).toBe(4)
    expect(screen.getByText('Обновления подключены')).toHaveAttribute('role', 'status')
  })

  it('ошибка перечитывания после разрыва повторяется, пока API не восстановится', async () => {
    const api = backend()
    render(<App />)
    await ready()
    openStream()
    expect(await screen.findByText('Обновления подключены')).toBeInTheDocument()
    let calls = 0
    api.on('GET /api/tasks', () => {
      calls += 1
      if (calls === 1) return apiError(500, 'internal_error', 'Внутренняя ошибка.')
      return json(
        200,
        tasks.map((task) =>
          task.id === 'tsk_101' ? { ...task, status: 'awaiting_integration', version: 9 } : task,
        ),
      )
    })
    act(() => FakeEventSource.last(STREAM).fail())
    await waitFor(() => expect(calls).toBe(1), SLOW)
    expect(FakeEventSource.all(STREAM)).toHaveLength(1)
    expect(screen.getByText('Переподключение к обновлениям…')).toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.all(STREAM)).toHaveLength(2), { timeout: 4000 })
    expect(calls).toBe(2)
    openStream()
    expect(await within(stack()).findByText('Ждёт слияния PR')).toBeInTheDocument()
    expect(await screen.findByText('Обновления подключены')).toBeInTheDocument()
  })

  it('ошибка перечитывания задачи по событию запускает восстановление', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    await screen.findByText('Обновления подключены')
    api.on('GET /api/tasks/tsk_101', () => apiError(500, 'internal_error', 'Внутренняя ошибка.'))
    const before = api.count('GET /api/tasks')
    act(() => source.emit(taskEvent('tsk_101', 'role_started', { step: 'qa' })))
    await waitFor(() => expect(source.closed).toBe(true))
    await waitFor(() => expect(api.count('GET /api/tasks')).toBe(before + 1), SLOW)
    await waitFor(() => expect(FakeEventSource.all(STREAM)).toHaveLength(2))
  })

  it('ошибка обновления по событию во время перечитывания после open повторяет перечитывание', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const slots = deferred<Response>()
    api.on('GET /api/slots', () => slots.promise)
    api.on('GET /api/tasks/tsk_101', () => apiError(500, 'internal_error', 'Внутренняя ошибка.'))
    const source = openStream()
    act(() => source.emit(taskEvent('tsk_101', 'status_changed', { from: 'developing', to: 'awaiting_integration' })))
    await waitFor(() => expect(api.count('GET /api/tasks/tsk_101')).toBe(1))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    const before = api.count('GET /api/tasks')
    api.on('GET /api/tasks', tasks.map((task) =>
      task.id === 'tsk_101' ? { ...task, status: 'awaiting_integration', version: 9 } : task,
    ))
    api.on('GET /api/slots', { used: 2, total: 3 })
    slots.resolve(json(200, { used: 2, total: 3 }))
    expect(await within(stack()).findByText('Ждёт слияния PR')).toBeInTheDocument()
    expect(api.count('GET /api/tasks')).toBe(before + 1)
    expect(await screen.findByText('Обновления подключены')).toBeInTheDocument()
    expect(source.closed).toBe(false)
  })

  it('ответы GET задачи в обратном порядке не откатывают версию', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    const first = deferred<Response>()
    let calls = 0
    api.on('GET /api/tasks/tsk_101', () => {
      calls += 1
      return calls === 1 ? first.promise : json(200, { ...developing, status: 'awaiting_integration', version: 7 })
    })
    act(() => source.emit(taskEvent('tsk_101', 'role_started', { step: 'qa' })))
    await waitFor(() => expect(calls).toBe(1))
    act(() => source.emit(taskEvent('tsk_101', 'role_finished', { step: 'qa' })))
    expect(await within(stack()).findByText('Ждёт слияния PR')).toBeInTheDocument()
    first.resolve(json(200, { ...developing, status: 'reviewing', version: 6 }))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(within(stack()).queryByText('Проверка')).not.toBeInTheDocument()
    expect(within(stack()).getByText('Ждёт слияния PR')).toBeInTheDocument()
  })

  it('запоздалый ответ, начатый до разрыва, не откатывает результат восстановления', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    const late = deferred<Response>()
    api.on('GET /api/tasks/tsk_101', () => late.promise)
    act(() => source.emit(taskEvent('tsk_101', 'role_started', { step: 'qa' })))
    await waitFor(() => expect(api.count('GET /api/tasks/tsk_101')).toBe(1))
    api.on('GET /api/tasks', tasks.map((task) =>
      task.id === 'tsk_101' ? { ...task, status: 'awaiting_integration', version: 8 } : task,
    ))
    act(() => source.fail())
    expect(await within(stack()).findByText('Ждёт слияния PR', {}, SLOW)).toBeInTheDocument()
    late.resolve(json(200, { ...developing, status: 'reviewing', version: 6 }))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(within(stack()).getByText('Ждёт слияния PR')).toBeInTheDocument()
  })

  it('ответы GET /api/slots в обратном порядке: применяется последний начатый', async () => {
    const api = backend()
    render(<App />)
    await ready()
    const source = openStream()
    await within(stack()).findByText('Занято слотов: 2 из 3')
    const first = deferred<Response>()
    let calls = 0
    api.on('GET /api/slots', () => {
      calls += 1
      return calls === 1 ? first.promise : json(200, { used: 3, total: 3 })
    })
    act(() => source.emit(taskEvent('tsk_101', 'status_changed', { from: 'a', to: 'b' })))
    await waitFor(() => expect(calls).toBe(1))
    act(() => source.emit(taskEvent('tsk_102', 'status_changed', { from: 'a', to: 'b' })))
    expect(await within(stack()).findByText('Занято слотов: 3 из 3')).toBeInTheDocument()
    first.resolve(json(200, { used: 1, total: 3 }))
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(within(stack()).getByText('Занято слотов: 3 из 3')).toBeInTheDocument()
  })
})

describe('постановка и решения в панели', () => {
  it('принятая задача добавляется в стек и выбирается', async () => {
    const api = backend()
    const created: Task = {
      ...developing,
      id: 'tsk_200',
      title: 'Новая задача из формы',
      status: 'queued',
      version: 1,
      created_at: '2026-10-24T12:00:00Z',
      spec: null,
    }
    api.on('POST /api/tasks', () => json(201, created))
    render(<App />)
    await ready()
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст задачи' }), {
      target: { value: 'Новая задача из формы для vibe-chat.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Поставить задачу' }))
    const item = await within(stack()).findByRole('button', { name: /Новая задача из формы/ })
    expect(item).toHaveAttribute('aria-current', 'true')
    expect(within(stack()).getAllByRole('button')[0]).toBe(item)
  })

  it('решение по задаче перечитывает задачу после ответа 409', async () => {
    const api = backend()
    api.on('POST /api/tasks/tsk_101/decisions', () =>
      apiError(409, 'version_mismatch', 'Задача изменилась. Обновите карточку и примите решение заново.'),
    )
    render(<App />)
    await ready()
    fireEvent.click(within(stack()).getByRole('button', { name: new RegExp(developing.title) }))
    const before = api.count('GET /api/tasks/tsk_101')
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Отменить задачу' }),
    )
    expect(await screen.findByText(/Задача изменилась/)).toBeInTheDocument()
    await waitFor(() => expect(api.count('GET /api/tasks/tsk_101')).toBe(before + 1))
  })

  it('событие начала слияния обновляет открытую карточку и блокирует отмену', async () => {
    const integrating: Task = {
      ...developing,
      status: 'awaiting_integration',
      pr_number: 7,
      pr_url: 'https://github.com/SlavaPush/vibe-studio/pull/7',
      head_sha: 'b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80',
      integration_started: true,
    }
    const api = backend()
    api.on('GET /api/tasks', tasks.map((task) => (task.id === developing.id ? integrating : task)))
    api.on('GET /api/tasks/tsk_101', integrating)
    render(<App />)
    await ready()
    const source = openStream()
    fireEvent.click(within(stack()).getByRole('button', { name: new RegExp(developing.title) }))
    expect(await screen.findByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
    api.on('GET /api/tasks/tsk_101', { ...integrating, merge_pending: true, version: integrating.version + 1 })
    act(() =>
      source.emit(taskEvent('tsk_101', 'merge_started', { pr_number: 7, head_sha: integrating.head_sha })),
    )
    await waitFor(() => expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeDisabled())
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toHaveAccessibleDescription(
      'Выполняется слияние PR задачи: отмена недоступна.',
    )
  })

  it('участник не видит активных действий по чужой задаче и по выпуску', async () => {
    backend()
    render(<App />)
    await ready()
    fireEvent.click(within(stack()).getByRole('button', { name: /Переключатель тёмной темы/ }))
    expect(await screen.findByRole('heading', { name: 'Переключатель тёмной темы в настройках' }))
      .toBeInTheDocument()
    const enabled = screen
      .getAllByRole('button')
      .filter((button) => !button.hasAttribute('disabled'))
      .map((button) => button.textContent)
    expect(enabled).not.toContain('Отменить задачу')
    expect(enabled).not.toContain('Подтвердить выпуск')
    expect(enabled).not.toContain('Откатить')
  })
})
