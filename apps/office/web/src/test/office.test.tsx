import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Participant, Task } from '@/api/types'
import { ReleaseQueue } from '@/components/office/ReleaseQueue'
import { TaskCard } from '@/components/office/TaskCard'
import { participants, releases, tasks } from '@/mocks/data'

const byId = (id: string) => participants.find((participant) => participant.id === id) as Participant
const developing = tasks.find((task) => task.id === 'tsk_101') as Task

describe('ReleaseQueue', () => {
  it('участник не видит кнопок подтверждения выпуска и отката', () => {
    render(<ReleaseQueue releases={releases} participant={byId('prt_maria')} />)
    expect(screen.queryByRole('button', { name: 'Подтвердить выпуск' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Откатить' })).not.toBeInTheDocument()
    expect(
      screen.getByText('Подтверждение выпуска и откат доступны только администратору.'),
    ).toBeInTheDocument()
  })

  it('администратор видит подтверждение выпуска и откат', () => {
    render(<ReleaseQueue releases={releases} participant={byId('prt_admin')} />)
    expect(screen.getByRole('button', { name: 'Подтвердить выпуск' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Откатить' })).toBeEnabled()
  })
})

describe('TaskCard', () => {
  const renderCard = (participant: Participant) =>
    render(
      <TaskCard
        task={developing}
        participant={participant}
        events={[]}
        report={undefined}
        diff={undefined}
      />,
    )

  it('владелец может отменить задачу', () => {
    renderCard(byId('prt_maria'))
    expect(screen.getByRole('button', { name: 'Отменить задачу' })).toBeEnabled()
  })

  it('отчёт без проверок и хешей показывает отсутствие проверок', () => {
    render(
      <TaskCard
        task={developing}
        participant={byId('prt_maria')}
        events={[]}
        report={{ task_id: developing.id, commit_sha: null, spec_hash: null, suite_hash: null, checks: [] }}
        diff={undefined}
      />,
    )
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Проверки' }), { button: 0 })
    expect(screen.getByText('Проверки ещё не запускались.', { exact: true })).toBeInTheDocument()
  })

  it('отчёт с проверками показывает отсутствующие хеши как «нет данных»', () => {
    render(
      <TaskCard
        task={developing}
        participant={byId('prt_maria')}
        events={[]}
        report={{
          task_id: developing.id,
          commit_sha: 'e7f8a9b0',
          spec_hash: null,
          suite_hash: null,
          checks: [{ command: 'npm run build', exit_code: 0, duration_ms: 1000, artifact_urls: [] }],
        }}
        diff={undefined}
      />,
    )
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Проверки' }), { button: 0 })
    expect(screen.getAllByText('нет данных', { exact: true })).toHaveLength(2)
  })

  it('другой участник видит заблокированное действие с пояснением', () => {
    renderCard(byId('prt_ivan'))
    const button = screen.getByRole('button', { name: 'Отменить задачу' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('Решения по задаче принимает только её владелец.')
  })
})
