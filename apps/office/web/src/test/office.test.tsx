import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Run } from '@/api/client'
import type { CheckReport, Participant, ReleaseCandidate, ReleaseQueueState, Task } from '@/api/types'
import { ReleaseQueue } from '@/components/office/ReleaseQueue'
import { TaskCard } from '@/components/office/TaskCard'
import { blockedRelease, participants, releaseQueue, releases, tasks } from '@/mocks/data'
import {
  FakeEventSource,
  apiError,
  fakeApi,
  installEventSource,
  json,
  taskEvent,
} from '@/test/fake-api'

const byId = (id: string) => participants.find((participant) => participant.id === id) as Participant
const maria = byId('prt_maria')
const admin = byId('prt_admin')
const ivan = byId('prt_ivan')
const developing = tasks.find((task) => task.id === 'tsk_101') as Task
const pending = releases.find((release) => release.status === 'pending') as ReleaseCandidate
const published = releases.find((release) => release.is_published) as ReleaseCandidate
const direct: Run = (operation) => operation()

const openTab = (name: string) =>
  fireEvent.mouseDown(screen.getByRole('tab', { name }), { button: 0 })

function renderCard(participant: Participant, task: Task = developing) {
  const onRefresh = vi.fn()
  const view = render(
    <TaskCard task={task} participant={participant} run={direct} onRefresh={onRefresh} />,
  )
  const rerender = (next: Task) =>
    view.rerender(<TaskCard task={next} participant={participant} run={direct} onRefresh={onRefresh} />)
  return { onRefresh, rerender }
}

describe('TaskCard: PR задачи', () => {
  const integrating: Task = {
    ...developing,
    status: 'awaiting_integration',
    pr_number: 7,
    pr_url: 'https://github.com/SlavaPush/vibe-studio/pull/7',
    head_sha: 'b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80',
    checks: {
      head_sha: 'b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80',
      checks: [
        { name: 'office-web', status: 'completed', conclusion: 'success', url: 'https://github.com/job/1' },
        { name: 'chat-api', status: 'completed', conclusion: 'failure', url: 'https://github.com/job/2' },
        { name: 'office-paths', status: 'in_progress', conclusion: null, url: null },
        { name: 'chat-web', status: 'missing', conclusion: null, url: null },
      ],
    },
  }

  it('показывает ссылку на PR и сводку обязательных проверок без вкладки журнала', () => {
    fakeApi()
    renderCard(maria, integrating)
    expect(screen.getByRole('link', { name: 'PR #7' })).toHaveAttribute('href', integrating.pr_url)
    const list = screen.getByRole('list', { name: 'Обязательные проверки PR' })
    const items = within(list).getAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      'office-websuccessЖурнал',
      'chat-apifailureЖурнал',
      'office-pathsВыполняется',
      'chat-webНет запуска',
    ])
    expect(within(list).getByRole('link', { name: 'Журнал проверки chat-api' })).toHaveAttribute(
      'href',
      'https://github.com/job/2',
    )
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(0)
  })

  it('показывает причину ошибки выпуска и запрещает повтор', () => {
    fakeApi()
    renderCard(maria, { ...integrating, status: 'failed', failure_kind: 'checks_failed' })
    expect(
      screen.getByText('Причина ошибки: Обязательные проверки PR не пройдены, PR закрыт.'),
    ).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: 'Повторить' })
    expect(retry).toBeDisabled()
    expect(retry).toHaveAccessibleDescription(
      'Обязательные проверки PR не пройдены, PR закрыт. Повтор недоступен: поставьте новую задачу.',
    )
  })

  it('отмена задачи с открытым PR предупреждает о закрытии PR', async () => {
    fakeApi()
    renderCard(maria, integrating)
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Отменить задачу?' })
    expect(dialog).toHaveTextContent('офис закрывает PR задачи')
  })

  const rejection = {
    id: 'prv_1',
    head_sha: integrating.head_sha as string,
    verdict: 'reject' as const,
    reasons: 'Добавлен запрос к внешнему API.',
    required_changes: ['Убрать fetch к внешнему адресу.', 'Оставить отправляемый текст без изменений.'],
    checklist: [
      { item: 'paths', ok: true, note: 'Только apps/chat/web/src.' },
      { item: 'network', ok: false, note: 'Новый fetch к https://example.com.' },
    ],
    created_at: '2026-10-24T10:00:00Z',
  }

  it('показывает отказ агента ревью отдельно от отказа фильтра приёма', () => {
    fakeApi()
    renderCard(maria, { ...integrating, status: 'failed', failure_kind: 'pr_rejected', pr_review: rejection })
    expect(screen.getByText('Причина ошибки: Агент ревью отклонил PR.')).toBeInTheDocument()
    const section = screen.getByRole('region', { name: /Решение агента ревью PR/ })
    expect(within(section).getByText('Отклонён')).toBeInTheDocument()
    expect(within(section).getByText('Добавлен запрос к внешнему API.')).toBeInTheDocument()
    const changes = within(section).getByRole('list', { name: 'Что изменить' })
    expect(within(changes).getAllByRole('listitem').map((item) => item.textContent)).toEqual(
      rejection.required_changes,
    )
    const checklist = within(section).getByRole('list', { name: 'Чек-лист агента ревью' })
    expect(within(checklist).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'В порядкеИзменённые путиТолько apps/chat/web/src.',
      'ЗамечаниеНовые сетевые вызовыНовый fetch к https://example.com.',
    ])
    expect(screen.queryByText('Задача отклонена фильтром приёма')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Повторить' })).toBeDisabled()
  })

  it('после слияния показывает итоговый commit, проверки commit и запрещает отмену', () => {
    fakeApi()
    const merge = 'c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6'
    const merged: Task = {
      ...integrating,
      merge_sha: merge,
      pr_review: { ...rejection, verdict: 'approve', required_changes: [] },
      checks: { head_sha: merge, checks: [{ name: 'office-web', status: 'in_progress', conclusion: null, url: null }] },
    }
    renderCard(maria, merged)
    expect(screen.getByRole('link', { name: 'Итоговый commit c3d4e5f' })).toHaveAttribute(
      'href',
      `https://github.com/SlavaPush/vibe-studio/commit/${merge}`,
    )
    expect(screen.getByRole('list', { name: 'Обязательные проверки итогового commit' })).toBeInTheDocument()
    expect(screen.getByText('Принят')).toBeInTheDocument()
    const cancel = screen.getByRole('button', { name: 'Отменить задачу' })
    expect(cancel).toBeDisabled()
    expect(cancel).toHaveAccessibleDescription('PR задачи уже слит в main: отмена недоступна.')
  })

  it('провал итогового commit не называет PR закрытым', () => {
    fakeApi()
    renderCard(maria, {
      ...integrating,
      status: 'failed',
      failure_kind: 'checks_failed',
      merge_sha: 'c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6',
    })
    expect(
      screen.getByText(
        'Причина ошибки: Обязательные проверки или критический E2E итогового commit не пройдены.',
      ),
    ).toBeInTheDocument()
  })
})

describe('TaskCard: действия', () => {
  it('владелец отменяет задачу только после подтверждения в диалоге', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', { decision_id: 'dec_1' })
    const { onRefresh } = renderCard(maria)
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Отменить задачу?' })
    expect(api.count('POST /api/tasks/tsk_101/decisions')).toBe(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Отменить задачу' }))
    await screen.findByText('Решение записано.')
    const [body] = api.bodies('POST /api/tasks/tsk_101/decisions') as Record<string, unknown>[]
    expect(body.decision).toBe('cancel')
    expect(body.expected_version).toBe(developing.version)
    expect(String(body.idempotency_key).length).toBeGreaterThanOrEqual(8)
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('закрытие диалога не отправляет решение', async () => {
    const api = fakeApi()
    renderCard(maria)
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Закрыть' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(api.calls).toHaveLength(0)
  })

  it('другой участник видит заблокированное действие с пояснением', () => {
    fakeApi()
    renderCard(ivan)
    const button = screen.getByRole('button', { name: 'Отменить задачу' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('Решения по задаче принимает только её владелец.')
  })

  it('администратор не принимает решения по чужой задаче', () => {
    fakeApi()
    renderCard(admin)
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeDisabled()
  })

  it('повтор после сетевой ошибки отправляет то же тело, новое нажатие — новый ключ', async () => {
    const api = fakeApi()
    let calls = 0
    api.on('POST /api/tasks/tsk_101/decisions', () => {
      calls += 1
      if (calls === 1) throw new TypeError('Failed to fetch')
      return json(200, { decision_id: 'dec_2' })
    })
    const spec = (tasks.find((task) => task.spec) as Task).spec
    const waiting: Task = { ...developing, status: 'awaiting_spec_approval', spec, version: 3 }
    const { rerender } = renderCard(maria, waiting)
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить требования' }))
    const retry = await screen.findByRole('button', { name: 'Отправить решение повторно' })
    expect(screen.getByRole('alert')).toHaveTextContent('API офиса недоступен')
    rerender({ ...waiting, version: 4 })
    fireEvent.click(retry)
    await screen.findByText('Решение записано.')
    rerender({ ...waiting, version: 5 })
    fireEvent.click(screen.getByRole('button', { name: 'Утвердить требования' }))
    await waitFor(() => expect(calls).toBe(3))
    const [first, second, third] = api.bodies('POST /api/tasks/tsk_101/decisions') as Record<
      string,
      unknown
    >[]
    expect(second).toEqual(first)
    expect(first.expected_version).toBe(3)
    expect(third.expected_version).toBe(5)
    expect(third.idempotency_key).not.toBe(first.idempotency_key)
  })

  it('обрыв при чтении ответа решения предлагает повтор с тем же телом', async () => {
    const api = fakeApi()
    let calls = 0
    api.on('POST /api/tasks/tsk_101/decisions', () => {
      calls += 1
      if (calls > 1) return json(200, { decision_id: 'dec_3' })
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () => Promise.reject(new TypeError('network error')),
      } as unknown as Response
    })
    renderCard(maria, { ...developing, status: 'failed' })
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Отправить решение повторно' }))
    await screen.findByText('Решение записано.')
    const [first, second] = api.bodies('POST /api/tasks/tsk_101/decisions')
    expect(second).toEqual(first)
  })

  it.each([
    ['version_mismatch', 'Задача изменилась. Обновите карточку и примите решение заново.'],
    ['decision_not_allowed', 'Решение недопустимо в текущем статусе.'],
  ])('409 %s показывает сообщение и перечитывает задачу', async (code, message) => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', () => apiError(409, code, message))
    const { onRefresh } = renderCard(maria)
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Отменить задачу' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(onRefresh).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Отправить решение повторно' })).not.toBeInTheDocument()
  })

  it('403 показывает сообщение без повтора', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', () =>
      apiError(403, 'forbidden_not_owner', 'Решения по задаче принимает только её владелец.'),
    )
    const { onRefresh } = renderCard(maria, { ...developing, status: 'failed' })
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('принимает только её владелец')
    expect(onRefresh).not.toHaveBeenCalled()
  })
})

describe('TaskCard: вкладки', () => {
  it('показывает diff', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks/tsk_101/diff', () => new Response('diff --git a/x b/x\n+new\n'))
    renderCard(maria)
    openTab('Изменения')
    expect(await screen.findByLabelText('Unified diff')).toHaveTextContent('diff --git a/x b/x')
  })

  it('404 diff — рабочая копия ещё не создана', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks/tsk_101/diff', () =>
      apiError(404, 'not_found', 'Рабочая копия задачи не найдена.'),
    )
    renderCard(maria)
    openTab('Изменения')
    expect(await screen.findByText('Рабочая копия задачи ещё не создана.')).toBeInTheDocument()
  })

  it('413 diff показывает сообщение и список изменённых файлов', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks/tsk_101/diff', () =>
      apiError(413, 'diff_too_large', 'Diff больше 1 МиБ. Список файлов доступен в карточке задачи.'),
    )
    renderCard(maria)
    openTab('Изменения')
    expect(await screen.findByRole('alert')).toHaveTextContent('Diff больше 1 МиБ')
    const files = screen.getByRole('list', { name: 'Изменённые файлы' })
    for (const file of developing.changed_files) expect(within(files).getByText(file)).toBeInTheDocument()
  })

  it('diff перечитывается при смене версии задачи', async () => {
    const api = fakeApi()
    let version = 0
    api.on('GET /api/tasks/tsk_101/diff', () => {
      version += 1
      return new Response(`diff v${version}`)
    })
    const { rerender } = renderCard(maria)
    openTab('Изменения')
    expect(await screen.findByLabelText('Unified diff')).toHaveTextContent('diff v1')
    rerender({ ...developing, version: developing.version + 1 })
    await waitFor(() => expect(screen.getByLabelText('Unified diff')).toHaveTextContent('diff v2'))
  })

  it('отчёт без проверок показывает отсутствие проверок', async () => {
    const api = fakeApi()
    const empty: CheckReport = {
      task_id: developing.id,
      commit_sha: null,
      spec_hash: null,
      suite_hash: null,
      checks: [],
    }
    api.on('GET /api/tasks/tsk_101/checks', empty)
    renderCard(maria)
    openTab('Проверки')
    expect(await screen.findByText('Проверки ещё не запускались.', { exact: true })).toBeInTheDocument()
  })

  it('отчёт показывает null как «нет данных» и ссылки на артефакты', async () => {
    const api = fakeApi()
    api.on('GET /api/tasks/tsk_101/checks', {
      task_id: developing.id,
      commit_sha: 'e7f8a9b0',
      spec_hash: null,
      suite_hash: null,
      checks: [
        {
          command: 'npm run build',
          exit_code: 0,
          duration_ms: 1000,
          artifact_urls: ['/api/tasks/tsk_101/artifacts/build.log'],
        },
      ],
    } satisfies CheckReport)
    renderCard(maria)
    openTab('Проверки')
    const link = await screen.findByRole('link', { name: 'build.log' })
    expect(link).toHaveAttribute('href', '/api/tasks/tsk_101/artifacts/build.log')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getAllByText('нет данных', { exact: true })).toHaveLength(2)
  })

  it('журнал берёт события из потока задачи без дублей', async () => {
    installEventSource()
    fakeApi()
    renderCard(maria)
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(0)
    openTab('Ход работы')
    const source = FakeEventSource.last('/api/tasks/tsk_101/events')
    const first = { ...taskEvent('tsk_101', 'task_created', { title: 'x' }), seq: 1 }
    const second = { ...taskEvent('tsk_101', 'role_started', { step: 'spec' }), seq: 2 }
    act(() => {
      source.emit(second)
      source.emit(first)
      source.emit(second)
    })
    const items = within(screen.getByRole('list', { name: 'События задачи' })).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Задача создана')
    expect(items[1]).toHaveTextContent('Роль начала работу')
    openTab('Требования')
    expect(source.closed).toBe(true)
  })

  it('журнал показывает состояние подключения и пустой журнал после open', () => {
    installEventSource()
    fakeApi()
    renderCard(maria)
    openTab('Ход работы')
    expect(screen.getByText('Подключение к журналу…')).toHaveAttribute('role', 'status')
    expect(screen.queryByText('Событий пока нет.')).not.toBeInTheDocument()
    act(() => FakeEventSource.last('/api/tasks/tsk_101/events').open())
    expect(screen.getByText('Событий пока нет.')).toBeInTheDocument()
    act(() => FakeEventSource.last('/api/tasks/tsk_101/events').fail())
    expect(screen.getByText('Журнал переподключается…')).toBeInTheDocument()
  })
})

const freeQueue: ReleaseQueueState = { ...releaseQueue, release_blocked_by: null, blocked_reason: null }

function renderQueue(
  participant: Participant,
  list: ReleaseCandidate[] = releases,
  queue: ReleaseQueueState | null = freeQueue,
) {
  const onRefresh = vi.fn()
  const view = render(
    <ReleaseQueue releases={list} queue={queue} participant={participant} run={direct} onRefresh={onRefresh} />,
  )
  const rerender = (next: ReleaseCandidate[]) =>
    view.rerender(
      <ReleaseQueue releases={next} queue={queue} participant={participant} run={direct} onRefresh={onRefresh} />,
    )
  return { onRefresh, rerender }
}

describe('ReleaseQueue', () => {
  it('участник не видит кнопок отката и возобновления, подтверждения выпуска нет', () => {
    fakeApi()
    renderQueue(maria, [...releases, blockedRelease], releaseQueue)
    expect(screen.queryByRole('button', { name: 'Подтвердить выпуск' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Откатить' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Возобновить выпуск' })).not.toBeInTheDocument()
    expect(screen.getByText('Откат и возобновление выпуска доступны только администратору.')).toBeInTheDocument()
    expect(screen.getByText('Ждёт очереди')).toBeInTheDocument()
  })

  it('показывает проверку после деплоя, откат и блокировку с revert-PR', () => {
    fakeApi()
    renderQueue(maria, [...releases, blockedRelease], releaseQueue)
    const blocked = screen.getByRole('region', { name: 'Выпуск заблокирован после отката rel_5' })
    expect(within(blocked).getByRole('link', { name: 'PR #11' })).toHaveAttribute(
      'href',
      'https://github.com/SlavaPush/vibe-studio/pull/11',
    )
    expect(within(blocked).getByText(/состояние: выполняется/)).toBeInTheDocument()
    expect(screen.getByText('не пройдена: /healthz не ответил status ok')).toBeInTheDocument()
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === 'DD' &&
          element.textContent === 'к 5e0c1a7, Render: live, проверка пройдена (попыток: 1)',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('пройдена (попыток: 2)')).toBeInTheDocument()
  })

  it('предупреждает, что выпуск не настроен', () => {
    fakeApi()
    renderQueue(admin, releases, { ...freeQueue, configured: false })
    expect(screen.getByText(/Выпуск не настроен/)).toHaveAttribute('role', 'status')
  })

  it('администратор возобновляет выпуск с fix_sha после блокировки', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${blockedRelease.id}/decisions`, { decision_id: 'dec_12' })
    const { onRefresh } = renderQueue(admin, [...releases, blockedRelease], releaseQueue)
    const button = screen.getByRole('button', { name: 'Возобновить выпуск' })
    const input = screen.getByLabelText('Исправляющий commit в main (fix_sha, 40 символов)')
    fireEvent.change(input, { target: { value: 'abc' } })
    expect(button).toBeDisabled()
    fireEvent.change(input, { target: { value: 'A'.repeat(40) } })
    fireEvent.click(button)
    await screen.findByText('Решение записано.')
    const [body] = api.bodies(`POST /api/releases/${blockedRelease.id}/decisions`) as Record<string, unknown>[]
    expect(body).toMatchObject({
      decision: 'resume_releases',
      expected_version: blockedRelease.version,
      fix_sha: 'a'.repeat(40),
    })
    expect(typeof body.idempotency_key).toBe('string')
    expect(onRefresh).toHaveBeenCalledWith(blockedRelease)
  })

  it('администратор повторяет revert-PR, требующий внимания, без fix_sha', async () => {
    const api = fakeApi()
    const stopped: ReleaseCandidate = {
      ...blockedRelease,
      revert: { ...blockedRelease.revert!, status: 'needs_attention', reason: 'Конфликт revert.' },
    }
    api.on(`POST /api/releases/${stopped.id}/decisions`, { decision_id: 'dec_14' })
    renderQueue(admin, [...releases, stopped], releaseQueue)
    fireEvent.click(screen.getByRole('button', { name: 'Повторить revert-PR' }))
    const dialog = await screen.findByRole('alertdialog', { name: `Повторить revert-PR выпуска ${stopped.id}?` })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Повторить revert-PR' }))
    await screen.findByText('Решение записано.')
    const [body] = api.bodies(`POST /api/releases/${stopped.id}/decisions`) as Record<string, unknown>[]
    expect(body).toMatchObject({ decision: 'resume_releases', expected_version: stopped.version })
    expect(body).not.toHaveProperty('fix_sha')
  })

  it('повтор revert-PR недоступен, пока revert выполняется', () => {
    fakeApi()
    renderQueue(admin, [...releases, blockedRelease], releaseQueue)
    expect(screen.queryByRole('button', { name: 'Повторить revert-PR' })).not.toBeInTheDocument()
  })

  it('остановленный кандидат возобновляется без fix_sha после подтверждения', async () => {
    const api = fakeApi()
    const halted = { ...pending, blocked_reason: 'Состав выпуска не подтверждён: c3d4e5f: нет push.' }
    api.on(`POST /api/releases/${pending.id}/decisions`, { decision_id: 'dec_13' })
    renderQueue(admin, [halted, published])
    expect(screen.getByText(halted.blocked_reason)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Возобновить выпуск' }))
    const dialog = await screen.findByRole('alertdialog', { name: `Возобновить выпуск ${pending.id}?` })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Возобновить выпуск' }))
    await screen.findByText('Решение записано.')
    const [body] = api.bodies(`POST /api/releases/${pending.id}/decisions`) as Record<string, unknown>[]
    expect(body).toMatchObject({ decision: 'resume_releases', expected_version: pending.version })
    expect(body).not.toHaveProperty('fix_sha')
  })

  it('откат требует подтверждения в диалоге', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, { decision_id: 'dec_10' })
    renderQueue(admin)
    fireEvent.click(screen.getByRole('button', { name: 'Откатить' }))
    const dialog = await screen.findByRole('alertdialog', { name: `Откатить выпуск ${published.id}?` })
    expect(api.calls).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Откатить' }))
    await screen.findByText('Решение записано.')
    expect(api.bodies(`POST /api/releases/${published.id}/decisions`)).toEqual([
      expect.objectContaining({ decision: 'rollback', expected_version: published.version }),
    ])
  })

  it('повтор после сетевой ошибки отправляет то же тело после обновления версии', async () => {
    const api = fakeApi()
    let calls = 0
    api.on(`POST /api/releases/${published.id}/decisions`, () => {
      calls += 1
      if (calls === 1) throw new TypeError('Failed to fetch')
      return json(200, { decision_id: 'dec_11' })
    })
    const { rerender } = renderQueue(admin)
    await confirmRollback()
    const retry = await screen.findByRole('button', { name: 'Отправить решение повторно' })
    rerender(releases.map((release) => (release.id === published.id ? { ...release, version: 9 } : release)))
    fireEvent.click(retry)
    await screen.findByText('Решение записано.')
    const [first, second] = api.bodies(`POST /api/releases/${published.id}/decisions`)
    expect(second).toEqual(first)
    expect(first).toMatchObject({ expected_version: published.version })
  })

  it.each([
    [403, 'forbidden_role', 'Откат и возобновление выпуска доступны только администратору.', false],
    [409, 'version_mismatch', 'Кандидат выпуска изменился. Проверьте новую версию и примите решение заново.', true],
    [409, 'decision_not_allowed', 'Решение недопустимо в текущем статусе.', true],
  ])('%i %s — сообщение у кандидата', async (status, code, message, refresh) => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, () => apiError(status, code, message))
    const { onRefresh } = renderQueue(admin)
    const item = screen.getByRole('button', { name: 'Откатить' }).closest('li') as HTMLElement
    await confirmRollback()
    expect(await within(item).findByRole('alert')).toHaveTextContent(message)
    if (refresh) expect(onRefresh).toHaveBeenCalledWith(published)
    else expect(onRefresh).not.toHaveBeenCalled()
  })
})

async function confirmRollback() {
  fireEvent.click(screen.getByRole('button', { name: 'Откатить' }))
  const dialog = await screen.findByRole('alertdialog', { name: `Откатить выпуск ${published.id}?` })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Откатить' }))
}
