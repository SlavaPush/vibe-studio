import { render, screen } from '@testing-library/react'
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

  it('другой участник видит заблокированное действие с пояснением', () => {
    renderCard(byId('prt_ivan'))
    const button = screen.getByRole('button', { name: 'Отменить задачу' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('Решения по задаче принимает только её владелец.')
  })
})
