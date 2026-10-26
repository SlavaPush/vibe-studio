import { act, cleanup as unmountAll, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Run } from '@/api/client'
import type { CheckReport, Participant, ReleaseCandidate, ReleaseQueueState, Task, TaskEvent } from '@/api/types'
import { ReleaseQueue } from '@/components/office/ReleaseQueue'
import { TaskCard } from '@/components/office/TaskCard'
import { DECISION_SLOW_MS } from '@/lib/use-decision'
import { blockedRelease, participants, releaseQueue, releases, tasks } from '@/mocks/data'
import {
  FakeEventSource,
  apiError,
  deferred,
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
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(1)
  })

  it('показывает доработку после отката с номером попытки и причиной', () => {
    fakeApi()
    const reason = 'Ручной откат администратора: Кнопка «Выйти» не завершает сеанс. Опубликована прежняя версия 8beae56.'
    renderCard(maria, { ...developing, branch: 'office/tsk_101-r1', rework_count: 1, rework_limit: 1, rework_reason: reason })
    expect(screen.getByRole('region', { name: 'Доработка после отката: попытка 1 из 1' })).toBeInTheDocument()
    expect(screen.getByText(`Причина: ${reason}`)).toBeInTheDocument()
  })

  it('показывает причину отката задачи до доработки и подпись rolled_back', () => {
    fakeApi()
    renderCard(maria, {
      ...integrating, status: 'failed', failure_kind: 'rolled_back', merge_sha: 'c'.repeat(40), rework_reason: 'Откат.',
    })
    expect(screen.getByRole('region', { name: 'Выпуск задачи остановлен' })).toBeInTheDocument()
    expect(screen.getByText('Причина ошибки: Выпуск откатан администратором.')).toBeInTheDocument()
  })

  it('без доработки плашки нет', () => {
    fakeApi()
    renderCard(maria)
    expect(screen.queryByRole('region', { name: /Доработка после отката/ })).not.toBeInTheDocument()
  })

  const halted: Task = {
    ...developing,
    status: 'needs_attention',
    halt_reason: 'Разработчик не изменил файлы. Разработчик сообщил: Выход требует серверного эндпоинта.',
  }

  it('показывает причину остановки и подсказку о действиях заказчику', () => {
    fakeApi()
    renderCard(maria, halted)
    const block = screen.getByRole('region', { name: 'Причина остановки' })
    expect(block).toHaveTextContent(halted.halt_reason as string)
    expect(block).toHaveTextContent(
      '«Повторить» запускает задачу заново с текущими требованиями. «Отменить задачу» завершает задачу.',
    )
  })

  it('участник без прав видит причину остановки без подсказки', () => {
    fakeApi()
    renderCard(ivan, halted)
    const block = screen.getByRole('region', { name: 'Причина остановки' })
    expect(block).toHaveTextContent(halted.halt_reason as string)
    expect(block).not.toHaveTextContent('Повторить')
  })

  it('ошибка выпуска: причина остановки после причины ошибки, без недоступных действий', () => {
    fakeApi()
    renderCard(maria, { ...integrating, status: 'failed', failure_kind: 'checks_failed', halt_reason: 'chat-web: failure.' })
    const failure = screen.getByText('Причина ошибки: Обязательные проверки PR не пройдены, PR закрыт.')
    const block = screen.getByRole('region', { name: 'Причина остановки' })
    expect(failure.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(block).toHaveTextContent('chat-web: failure.')
    expect(block).not.toHaveTextContent('Повторить')
  })

  it('без halt_reason и в рабочем статусе блока причины нет', () => {
    fakeApi()
    renderCard(maria, { ...developing, halt_reason: 'Старая причина.' })
    expect(screen.queryByRole('region', { name: 'Причина остановки' })).not.toBeInTheDocument()
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
    owner_summary: null,
  }

  const server: Task = {
    ...integrating,
    change_class: 'backend_api',
    contour: 'server',
    pr_review: {
      ...rejection,
      verdict: 'approve',
      required_changes: [],
      owner_summary: 'Добавлено поле message_count. Рекомендация: одобрить.',
    },
    owner_review: { state: 'requested', head_sha: integrating.head_sha as string, login: null, comment: null },
  }

  it('показывает контур, сводку для владельца и ожидание одобрения владельца', () => {
    fakeApi()
    renderCard(maria, server)
    expect(screen.getByText('Контур: Сервер')).toBeInTheDocument()
    const review = screen.getByRole('region', { name: /Решение агента ревью PR/ })
    expect(within(review).getByText('Сводка для владельца')).toBeInTheDocument()
    expect(within(review).getByText('Добавлено поле message_count. Рекомендация: одобрить.')).toBeInTheDocument()
    const owner = screen.getByRole('region', { name: 'Одобрение владельца' })
    expect(within(owner).getByRole('status')).toHaveTextContent('Ждёт одобрения владельца на GitHub')
    expect(within(owner).getByRole('link', { name: 'PR #7' })).toHaveAttribute('href', server.pr_url)
  })

  it('показывает отказ владельца с его комментарием', () => {
    fakeApi()
    renderCard(maria, {
      ...server,
      status: 'failed',
      failure_kind: 'owner_rejected',
      owner_review: {
        state: 'changes_requested',
        head_sha: integrating.head_sha as string,
        login: 'SlavaPush',
        comment: 'Нужна пагинация',
      },
    })
    expect(screen.getByText('Причина ошибки: Владелец отклонил PR, PR закрыт.')).toBeInTheDocument()
    const owner = screen.getByRole('region', { name: 'Одобрение владельца' })
    expect(within(owner).getByText('Комментарий владельца: Нужна пагинация')).toBeInTheDocument()
    expect(owner).toHaveTextContent('Владелец SlavaPush отклонил')
    expect(screen.getByRole('button', { name: 'Повторить' })).toBeDisabled()
  })

  it('задача интерфейса показывает контур интерфейса без блока владельца', () => {
    fakeApi()
    renderCard(maria, { ...integrating, contour: 'ui' })
    expect(screen.getByText('Контур: Интерфейс')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Одобрение владельца' })).not.toBeInTheDocument()
  })

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
    expect(button).toHaveAccessibleDescription('Решения по задаче принимает только её заказчик.')
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
      apiError(403, 'forbidden_not_owner', 'Решения по задаче принимает только её заказчик.'),
    )
    const { onRefresh } = renderCard(maria, { ...developing, status: 'failed' })
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('принимает только её заказчик')
    expect(onRefresh).not.toHaveBeenCalled()
  })

  async function cancelTask() {
    fireEvent.click(screen.getByRole('button', { name: 'Отменить задачу' }))
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Отменить задачу' }),
    )
  }

  it('после отмены кнопки решений неактивны до прихода новой версии задачи', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', { decision_id: 'dec_20' })
    const { onRefresh, rerender } = renderCard(maria)
    await cancelTask()
    await screen.findByText('Решение записано.')
    const button = screen.getByRole('button', { name: 'Отменить задачу' })
    expect(button).toBeDisabled()
    expect(onRefresh).toHaveBeenCalledTimes(1)

    act(() => {
      vi.advanceTimersByTime(DECISION_SLOW_MS)
    })
    expect(screen.getByText('Решение записано, обновляем задачу…')).toHaveAttribute('role', 'status')
    expect(onRefresh).toHaveBeenCalledTimes(2)
    expect(button).toBeDisabled()

    rerender({ ...developing })
    expect(button).toBeDisabled()
    rerender({ ...developing, version: developing.version + 1 })
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
    expect(screen.getByText('Решение записано.')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(DECISION_SLOW_MS)
    })
    expect(onRefresh).toHaveBeenCalledTimes(2)
  })

  it('ожидание новой версии сохраняется после повторного открытия карточки', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', { decision_id: 'dec_23' })
    renderCard(maria)
    await cancelTask()
    await screen.findByText('Решение записано.')
    unmountAll()
    const { rerender } = renderCard(maria)
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeDisabled()
    expect(screen.getByText('Решение записано.')).toBeInTheDocument()
    rerender({ ...developing, version: developing.version + 1 })
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
    unmountAll()
    renderCard(maria, { ...developing, version: developing.version + 1 })
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
  })

  it('карточка, открытая заново до ответа на решение, получает блокировку, сообщение и повторную загрузку', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const api = fakeApi()
    const answer = deferred<Response>()
    api.on('POST /api/tasks/tsk_101/decisions', () => answer.promise)
    renderCard(maria)
    await cancelTask()
    await waitFor(() => expect(api.count('POST /api/tasks/tsk_101/decisions')).toBe(1))
    unmountAll()
    const { onRefresh } = renderCard(maria)
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeDisabled()
    expect(screen.queryByText('Решение записано.')).not.toBeInTheDocument()
    await act(async () => {
      answer.resolve(json(200, { decision_id: 'dec_25' }))
    })
    expect(await screen.findByText('Решение записано.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeDisabled()
    act(() => {
      vi.advanceTimersByTime(DECISION_SLOW_MS)
    })
    expect(onRefresh).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Решение записано, обновляем задачу…')).toBeInTheDocument()
  })

  it('ошибка решения снимает блокировку кнопок', async () => {
    const api = fakeApi()
    api.on('POST /api/tasks/tsk_101/decisions', () => apiError(500, 'internal_error', 'Внутренняя ошибка.'))
    renderCard(maria)
    await cancelTask()
    expect(await screen.findByRole('alert')).toHaveTextContent('Внутренняя ошибка API офиса')
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
  })
})

describe('TaskCard: название и описание', () => {
  const long: Task = {
    ...developing,
    title: 'Подсказка в пустом диалоге',
    text: 'В пустом диалоге покажи подсказку.\nВторая строка.\nТретья строка.\nЧетвёртая строка.\nПятая строка.',
  }

  function stubOverflow(overflow: boolean) {
    const height = vi.spyOn(Element.prototype, 'scrollHeight', 'get').mockReturnValue(overflow ? 200 : 80)
    const client = vi.spyOn(Element.prototype, 'clientHeight', 'get').mockReturnValue(80)
    return () => {
      height.mockRestore()
      client.mockRestore()
    }
  }

  it('показывает название в заголовке, заказчика и полный текст в блоке «Описание»', () => {
    fakeApi()
    renderCard(maria, long)
    expect(screen.getByRole('heading', { level: 2, name: long.title })).toBeInTheDocument()
    expect(screen.getByText(`Заказчик: ${long.owner_name}`)).toBeInTheDocument()
    expect(screen.queryByText(/Владелец:/)).not.toBeInTheDocument()
    const description = screen.getByRole('region', { name: 'Описание' })
    const text = within(description).getByText((_, element) => element?.tagName === 'P' && element.textContent === long.text)
    expect(text).toHaveClass('whitespace-pre-wrap', 'line-clamp-4')
    expect(within(description).queryByRole('button')).not.toBeInTheDocument()
    openTab('Требования')
    expect(screen.queryByText(/Текст задачи:/)).not.toBeInTheDocument()
  })

  it('длинный текст свёрнут и раскрывается кнопкой', () => {
    const restore = stubOverflow(true)
    fakeApi()
    renderCard(maria, long)
    const description = screen.getByRole('region', { name: 'Описание' })
    const toggle = within(description).getByRole('button', { name: 'Показать полностью' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    const collapse = within(description).getByRole('button', { name: 'Свернуть' })
    expect(collapse).toHaveAttribute('aria-expanded', 'true')
    expect(description.querySelector('#task-description')).not.toHaveClass('line-clamp-4')
    fireEvent.click(collapse)
    expect(description.querySelector('#task-description')).toHaveClass('line-clamp-4')
    restore()
  })

  it.each(['rejected', 'awaiting_spec_approval', 'released'] as const)('описание видно при статусе %s', (status) => {
    fakeApi()
    renderCard(maria, { ...long, status, spec: null, rejection_reason: status === 'rejected' ? 'Не про чат.' : null })
    expect(screen.getByRole('region', { name: 'Описание' })).toHaveTextContent('Пятая строка.')
  })
})

describe('TaskCard: вкладки', () => {
  it('требования показывают план реализации, а карточка — оценку стоимости без профиля', () => {
    fakeApi()
    renderCard(maria)
    expect(screen.queryByText(/Профиль/)).not.toBeInTheDocument()
    openTab('Требования')
    const heading = screen.getByRole('heading', { name: 'План реализации', level: 3 })
    const steps = within(heading.parentElement as HTMLElement).getAllByRole('listitem')
    expect(steps.map((step) => step.textContent)).toEqual(developing.spec?.plan)
    openTab('Расход')
    expect(screen.getByText('Оценка стоимости')).toBeInTheDocument()
    expect(screen.getByText('$0.1876')).toBeInTheDocument()
  })

  it('требования без плана не показывают блок плана', () => {
    fakeApi()
    renderCard(maria, tasks.find((task) => task.id === 'tsk_102') as Task)
    openTab('Требования')
    expect(screen.getByRole('heading', { name: 'Критерии приёмки', level: 3 })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'План реализации' })).not.toBeInTheDocument()
  })

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
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(1)
    openTab('Ход работы')
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(1)
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
    expect(items[0]).toHaveTextContent('начал работу')
    expect(items[1]).toHaveTextContent('Задача создана')
    openTab('Требования')
    expect(source.closed).toBe(false)
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(1)
  })

  it('у выпущенной задачи поток открыт только на вкладке «Ход работы»', () => {
    fakeApi()
    renderCard(maria, { ...developing, status: 'released' })
    expect(FakeEventSource.all('/api/tasks/tsk_101/events')).toHaveLength(0)
    openTab('Ход работы')
    const source = FakeEventSource.last('/api/tasks/tsk_101/events')
    openTab('Требования')
    expect(source.closed).toBe(true)
  })

  const nowLine = () => screen.queryByText('Сейчас:', { exact: false })?.closest('div') ?? null

  it('строка «Сейчас» видна на вкладке «Требования» у задачи в разработке, время растёт, после 10 минут — пометка', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-24T10:02:15Z'))
    fakeApi()
    renderCard(maria)
    expect(screen.getByRole('tab', { name: 'Требования' })).toHaveAttribute('aria-selected', 'true')
    const source = FakeEventSource.last('/api/tasks/tsk_101/events')
    act(() => {
      source.open()
      source.emit(roleEvent('role_started', 'developer', { step: 'implement' }))
      source.emit(
        roleEvent(
          'tool_called',
          'developer',
          { arguments: 'developer/implement anthropic/claude-opus-5-5', provider: 'anthropic', model: 'claude-opus-5-5', result: 'ok' },
          'model',
        ),
      )
    })
    expect(nowLine()).toHaveTextContent('Сейчас: Разработчик: реализация — ждёт ответ anthropic/claude-opus-5-5')
    expect(nowLine()).toHaveTextContent('2 мин 15 с с последнего события')
    act(() => vi.advanceTimersByTime(2000))
    expect(nowLine()).toHaveTextContent('2 мин 17 с с последнего события')
    expect(nowLine()).not.toHaveTextContent('долго нет событий')
    act(() => vi.advanceTimersByTime(8 * 60 * 1000))
    expect(nowLine()).toHaveTextContent('10 мин 17 с с последнего события')
    expect(nowLine()).toHaveTextContent('долго нет событий')
  })

  it('строка «Сейчас»: role="status", aria-live и пульсирующая точка без анимации при reduced motion', () => {
    fakeApi()
    renderCard(maria)
    const line = nowLine() as HTMLElement
    expect(within(line).getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(line.querySelector('[aria-hidden="true"]')).toHaveClass('animate-pulse', 'motion-reduce:animate-none')
    expect(line).toHaveClass('flex-wrap')
  })

  it('у выпущенной задачи строки «Сейчас» нет', () => {
    fakeApi()
    renderCard(maria, { ...developing, status: 'released' })
    expect(nowLine()).toBeNull()
  })

  const roleEvent = (
    type: string,
    role: TaskEvent['role'],
    payload: Record<string, unknown>,
    tool: string | null = null,
  ): TaskEvent => ({ ...taskEvent('tsk_101', type, payload), role, tool })

  function openJournal(events: TaskEvent[], task: Task = developing) {
    installEventSource()
    fakeApi()
    const view = renderCard(maria, task)
    openTab('Ход работы')
    const source = FakeEventSource.last('/api/tasks/tsk_101/events')
    act(() => {
      source.open()
      for (const event of events) source.emit(event)
    })
    return view
  }

  it('схема конвейера: семь агентов, работающий выделен, передача озвучена', () => {
    openJournal([
      taskEvent('tsk_101', 'task_created', { title: 'x', provider: 'anthropic', model: 'claude-haiku-5-5', effort: 'low' }),
      roleEvent('role_started', 'supervisor', { step: 'spec' }),
      roleEvent('role_finished', 'supervisor', { step: 'spec', outcome: 'ok', summary: 'Требования: 3 критерия приёмки.' }),
      roleEvent('role_started', 'developer', { step: 'implement' }),
      roleEvent(
        'tool_called',
        'developer',
        {
          arguments: 'developer/implement anthropic/claude-sonnet-5-5',
          result: 'ok',
          provider: 'anthropic',
          model: 'claude-sonnet-5-5',
          effort: 'high',
        },
        'model',
      ),
      roleEvent('tool_called', 'developer', { arguments: 'apps/chat/web/src/ChatThread.tsx', result: 'ok' }, 'read_file'),
    ])
    const board = screen.getByRole('list', { name: 'Схема конвейера' })
    for (const name of ['Фильтр приёма', 'Supervisor', 'Исследователь', 'Разработчик', 'QA', 'Критик', 'Агент ревью PR']) {
      expect(within(board).getByRole('heading', { name })).toBeInTheDocument()
    }
    const developer = within(board).getByRole('heading', { name: 'Разработчик' }).closest('div.rounded-md') as HTMLElement
    expect(developer).toHaveClass('ring-2')
    expect(developer).toHaveTextContent('работает')
    expect(developer).toHaveTextContent('Модель: anthropic/claude-sonnet-5-5, усилие high')
    expect(developer).toHaveTextContent('читает ChatThread.tsx')
    expect(within(board).getByText('Supervisor передаёт требования → Разработчик, сейчас')).toHaveClass('sr-only')
    expect(within(board).getByText('не понадобился')).toBeInTheDocument()
    expect(board.querySelector('.animate-handoff')).toHaveClass('motion-reduce:animate-none')
    expect(screen.getByRole('region', { name: 'GitHub' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Выпуск' })).toHaveTextContent('Выпуск начнётся после слияния PR.')
  })

  it('лента: описание по-русски, вложенный payload в подробностях, неизвестный тип', () => {
    openJournal([
      taskEvent('tsk_101', 'checks_status', {
        head_sha: 'b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80',
        checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: 'https://github.com/run/1' }],
      }),
      taskEvent('tsk_101', 'owner_review_requested', { pr_number: 12, reviewers: ['SlavaPush'] }),
    ])
    const feed = screen.getByRole('list', { name: 'События задачи' })
    const items = within(feed).getAllByRole('listitem')
    expect(items[1]).toHaveTextContent('Обязательные проверки: 1 из 1 успешно для b7c8d9e')
    expect(items[0]).toHaveTextContent('Событие owner_review_requested')
    expect(feed.textContent).not.toContain('[object Object]')
    const details = within(items[1]).getByText('Подробности').closest('details') as HTMLElement
    expect(details.querySelector('pre')?.textContent).toContain('"url": "https://github.com/run/1"')
    expect(screen.getByRole('list', { name: 'Схема конвейера' })).toBeInTheDocument()
  })

  it('лента: причина в строке смены статуса, подробности подписями, JSON под «Данные»', () => {
    const reason = 'Разработчик не изменил файлы. Разработчик сообщил: Нужен DELETE /api/session.'
    openJournal([taskEvent('tsk_101', 'status_changed', { from: 'developing', to: 'needs_attention', reason })])
    const [item] = within(screen.getByRole('list', { name: 'События задачи' })).getAllByRole('listitem')
    expect(item).toHaveTextContent(`Статус: Требует внимания. Причина: ${reason}`)
    const details = within(item).getByText('Подробности').closest('details') as HTMLElement
    const terms = Array.from(details.querySelectorAll('dt')).map((node) => node.textContent)
    const values = Array.from(details.querySelectorAll('dd')).map((node) => node.textContent)
    expect(terms).toEqual(['Было', 'Стало', 'Причина'])
    expect(values).toEqual(['Разработка', 'Требует внимания', reason])
    const data = within(details).getByText('Данные').closest('details') as HTMLElement
    expect(data).not.toBe(details)
    expect(data).not.toHaveAttribute('open')
    expect(data.querySelector('pre')?.textContent).toContain('"to": "needs_attention"')
    expect(details.querySelector('dl')?.textContent).not.toContain('{')
  })

  const feedItems = () =>
    within(screen.getByRole('list', { name: 'События задачи' }))
      .getAllByRole('listitem')
      .map((item) => item.dataset.seq)

  function emit(...events: TaskEvent[]) {
    const source = FakeEventSource.last('/api/tasks/tsk_101/events')
    act(() => {
      for (const event of events) source.emit(event)
    })
  }

  it('лента в области прокрутки с ограниченной высотой, новые события сверху', () => {
    const first = taskEvent('tsk_101', 'task_created', { title: 'x' })
    const second = roleEvent('role_started', 'supervisor', { step: 'spec' })
    openJournal([first, second])
    const feed = screen.getByRole('region', { name: 'Лента событий' })
    expect(feed).toHaveAttribute('tabindex', '0')
    expect(feed).toHaveClass('max-h-[60vh]', 'desk:max-h-96', 'overflow-y-auto')
    expect(screen.getByRole('list', { name: 'Схема конвейера' }).closest('[role="region"]')).toBeNull()
    expect(feedItems()).toEqual([String(second.seq), String(first.seq)])

    const third = roleEvent('role_finished', 'supervisor', { step: 'spec', outcome: 'ok', summary: 'Готово.' })
    emit(third)
    expect(feedItems()).toEqual([String(third.seq), String(second.seq), String(first.seq)])
    expect(feed.scrollTop).toBe(0)
    expect(screen.queryByRole('button', { name: /Новые события/ })).not.toBeInTheDocument()
  })

  it('после прокрутки вниз сохраняет позицию и предлагает перейти к новым событиям', () => {
    const offset = vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function (this: HTMLElement) {
      return this.parentElement ? Array.from(this.parentElement.children).indexOf(this) * 100 : 0
    })
    openJournal([taskEvent('tsk_101', 'task_created', { title: 'x' }), roleEvent('role_started', 'supervisor', { step: 'spec' })])
    const feed = screen.getByRole('region', { name: 'Лента событий' })
    feed.scrollTop = 150
    fireEvent.scroll(feed)
    emit(roleEvent('role_started', 'developer', { step: 'implement' }), roleEvent('role_started', 'qa', { step: 'qa' }))
    expect(feed.scrollTop).toBe(350)
    const button = screen.getByRole('button', { name: 'Новые события (2)' })
    emit(roleEvent('role_started', 'critic', { step: 'review' }))
    expect(feed.scrollTop).toBe(450)
    expect(button).toHaveTextContent('Новые события (3)')
    fireEvent.click(button)
    expect(feed.scrollTop).toBe(0)
    expect(screen.queryByRole('button', { name: /Новые события/ })).not.toBeInTheDocument()

    feed.scrollTop = 300
    emit(roleEvent('role_finished', 'critic', { step: 'review', outcome: 'ok', summary: 'pass' }))
    expect(screen.getByRole('button', { name: 'Новые события (1)' })).toBeInTheDocument()
    feed.scrollTop = 0
    fireEvent.scroll(feed)
    expect(screen.queryByRole('button', { name: /Новые события/ })).not.toBeInTheDocument()
    offset.mockRestore()
  })

  it('индикатор новых событий на закрытой вкладке снимается открытием', () => {
    installEventSource()
    fakeApi()
    const view = render(<TaskCard task={developing} participant={maria} run={direct} onRefresh={vi.fn()} lastEventId={10} />)
    const card = (lastEventId: number, task: Task = developing) =>
      view.rerender(<TaskCard task={task} participant={maria} run={direct} onRefresh={vi.fn()} lastEventId={lastEventId} />)
    expect(screen.getByRole('tab', { name: 'Ход работы' })).toBeInTheDocument()
    card(11)
    const tab = screen.getByRole('tab', { name: 'Ход работы, есть новые события' })
    const dot = tab.querySelector('[aria-hidden="true"]')
    expect(dot).toHaveClass('bg-destructive', 'animate-pulse', 'motion-reduce:animate-none')
    openTab('Ход работы, есть новые события')
    expect(screen.getByRole('tab', { name: 'Ход работы' })).toBeInTheDocument()
    card(12)
    expect(screen.getByRole('tab', { name: 'Ход работы' })).toBeInTheDocument()
    openTab('Требования')
    expect(screen.getByRole('tab', { name: 'Ход работы' })).toBeInTheDocument()
    card(13)
    expect(screen.getByRole('tab', { name: 'Ход работы, есть новые события' })).toBeInTheDocument()
    openTab('Изменения')
    openTab('Проверки')
    expect(screen.getByRole('tab', { name: 'Ход работы, есть новые события' })).toBeInTheDocument()
  })

  it.each([
    { status: 'developing' as const, spec: true, pulse: true },
    { status: 'awaiting_spec_approval' as const, spec: false, pulse: true },
    { status: 'awaiting_spec_approval' as const, spec: true, pulse: false },
    { status: 'released' as const, spec: true, pulse: false },
  ])('индикатор пульсирует только на активном этапе: $status, требования $spec', ({ status, spec, pulse }) => {
    fakeApi()
    const task = { ...developing, status, spec: spec ? developing.spec : null }
    const view = render(<TaskCard task={task} participant={maria} run={direct} onRefresh={vi.fn()} lastEventId={1} />)
    view.rerender(<TaskCard task={task} participant={maria} run={direct} onRefresh={vi.fn()} lastEventId={2} />)
    const dot = screen.getByRole('tab', { name: 'Ход работы, есть новые события' }).querySelector('[aria-hidden="true"]')
    if (pulse) expect(dot).toHaveClass('animate-pulse')
    else expect(dot).not.toHaveClass('animate-pulse')
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

function rollbackOf(release: ReleaseCandidate, decisionId: string, status: ReleaseCandidate['status']): ReleaseCandidate {
  return {
    ...release,
    id: `rel_rollback_${decisionId}`,
    kind: 'rollback',
    origin: 'manual_rollback',
    origin_decision_id: decisionId,
    status,
    is_published: false,
    commit_sha: release.expected_previous_sha,
    expected_previous_sha: release.commit_sha,
  }
}

function renderQueue(
  participant: Participant,
  list: ReleaseCandidate[] = releases,
  queue: ReleaseQueueState | null = freeQueue,
) {
  const onRefresh = vi.fn()
  const onOpenTask = vi.fn()
  const queueView = (next: ReleaseCandidate[]) => (
    <ReleaseQueue
      releases={next}
      tasks={tasks}
      queue={queue}
      participant={participant}
      run={direct}
      onRefresh={onRefresh}
      onOpenTask={onOpenTask}
    />
  )
  const view = render(queueView(list))
  const rerender = (next: ReleaseCandidate[]) => view.rerender(queueView(next))
  return { onRefresh, onOpenTask, rerender }
}

describe('ReleaseQueue', () => {
  it('показывает название задачи кнопкой и ссылку на PR', () => {
    fakeApi()
    const { onOpenTask } = renderQueue(maria, releases)
    const task = tasks.find((item) => item.id === pending.task_id) as Task
    const queueSection = screen.getByRole('region', { name: 'Очередь выпуска' })
    const open = within(queueSection).getByRole('button', { name: `Открыть задачу «${task.title}»` })
    expect(open).toHaveTextContent(task.title)
    const link = within(queueSection).getByRole('link', { name: 'PR #12' })
    expect(link).toHaveAttribute('href', 'https://github.com/SlavaPush/vibe-studio/pull/12')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
    expect(link).toHaveClass('tap-target', 'min-w-10')
    fireEvent.click(open)
    expect(onOpenTask).toHaveBeenCalledWith(task.id)
  })

  it('задача вне списка выводится идентификатором без кнопки, PR без номера — «PR»', () => {
    fakeApi()
    const other: ReleaseCandidate = { ...published, pr_url: 'https://github.com/SlavaPush/vibe-studio/commit/abc' }
    renderQueue(maria, [pending, other])
    const history = screen.getByRole('region', { name: 'История выпусков' })
    expect(within(history).getByText(`Задача ${published.task_id}`)).toBeInTheDocument()
    expect(within(history).queryByRole('button', { name: /Открыть задачу/ })).not.toBeInTheDocument()
    expect(within(history).getByRole('link', { name: 'PR' })).toHaveAttribute('href', other.pr_url)
  })

  it('без pr_url ссылки на PR нет', () => {
    fakeApi()
    renderQueue(maria, [{ ...pending, pr_url: null }])
    const queueSection = screen.getByRole('region', { name: 'Очередь выпуска' })
    expect(within(queueSection).queryByRole('link')).not.toBeInTheDocument()
  })

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
    const revertLink = within(blocked).getByRole('link', { name: 'PR #11' })
    expect(revertLink).toHaveAttribute('href', 'https://github.com/SlavaPush/vibe-studio/pull/11')
    expect(revertLink).toHaveClass('tap-target', 'min-w-10')
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

  it('после возобновления кнопка неактивна до новой версии кандидата', async () => {
    const api = fakeApi()
    const halted = { ...pending, blocked_reason: 'Состав выпуска не подтверждён.' }
    api.on(`POST /api/releases/${pending.id}/decisions`, { decision_id: 'dec_21' })
    const { rerender } = renderQueue(admin, [halted, published])
    fireEvent.click(screen.getByRole('button', { name: 'Возобновить выпуск' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Возобновить выпуск' }))
    await screen.findByText('Решение записано.')
    expect(screen.getByRole('button', { name: 'Возобновить выпуск' })).toBeDisabled()
    rerender([{ ...halted, version: halted.version + 1 }, published])
    expect(screen.getByRole('button', { name: 'Возобновить выпуск' })).toBeEnabled()
  })

  it('после отката кнопка неактивна, пока откат в очереди, и снова активна после его ошибки', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, { decision_id: 'dec_22' })
    const { rerender } = renderQueue(admin, [published])
    await confirmRollback()
    await screen.findByText('Решение записано.')
    const rollback = () => screen.getByRole('button', { name: 'Откатить' })
    expect(rollback()).toBeDisabled()
    const queued = rollbackOf(published, 'dec_22', 'pending')
    rerender([{ ...queued, id: 'rel_old', origin_decision_id: 'dec_old', status: 'failed' }, published])
    expect(rollback()).toBeDisabled()
    rerender([queued, published])
    expect(rollback()).toBeDisabled()
    rerender([{ ...queued, status: 'failed', version: queued.version + 1 }, published])
    expect(rollback()).toBeEnabled()
  })

  it('кнопка отката активна, если первое обновление уже содержит неудавшийся откат этого решения', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, { decision_id: 'dec_24' })
    const { rerender } = renderQueue(admin, [published])
    await confirmRollback()
    await screen.findByText('Решение записано.')
    expect(screen.getByRole('button', { name: 'Откатить' })).toBeDisabled()
    rerender([rollbackOf(published, 'dec_24', 'failed'), published])
    expect(screen.getByRole('button', { name: 'Откатить' })).toBeEnabled()
  })

  it('ошибка решения выпуска снимает блокировку', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, () => apiError(500, 'internal_error', 'Внутренняя ошибка.'))
    renderQueue(admin)
    await confirmRollback()
    expect(await screen.findByRole('alert')).toHaveTextContent('Внутренняя ошибка API офиса')
    expect(screen.getByRole('button', { name: 'Откатить' })).toBeEnabled()
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

  it('откат отправляет причину из поля формы и показывает причину у кандидата', async () => {
    const api = fakeApi()
    api.on(`POST /api/releases/${published.id}/decisions`, { decision_id: 'dec_14' })
    const { rerender } = renderQueue(admin)
    const field = screen.getByLabelText('Причина отката (необязательно, до 500 символов)')
    expect(field).toHaveAttribute('maxLength', '500')
    fireEvent.change(field, { target: { value: '  Кнопка «Выйти» не завершает сеанс.  ' } })
    await confirmRollback()
    await screen.findByText('Решение записано.')
    expect(api.bodies(`POST /api/releases/${published.id}/decisions`)).toEqual([
      expect.objectContaining({ decision: 'rollback', reason: 'Кнопка «Выйти» не завершает сеанс.' }),
    ])
    rerender([{ ...published, reason: 'Кнопка «Выйти» не завершает сеанс.' }])
    expect(screen.getByText('Кнопка «Выйти» не завершает сеанс.', { selector: 'dd' })).toBeInTheDocument()
  })

  it('поле причины есть только у отката', () => {
    fakeApi()
    renderQueue(admin, [{ ...pending, blocked_reason: 'Остановлен.' }])
    expect(screen.queryByLabelText(/Причина отката/)).not.toBeInTheDocument()
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
