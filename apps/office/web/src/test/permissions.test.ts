import { describe, expect, it } from 'vitest'
import type { Participant, ReleaseCandidate, Task } from '@/api/types'
import { canAct } from '@/lib/permissions'
import { participants, releases, tasks } from '@/mocks/data'

const byId = (id: string) => participants.find((participant) => participant.id === id) as Participant
const admin = byId('prt_admin')
const owner = byId('prt_maria')
const observer = byId('prt_ivan')
const developing = tasks.find((task) => task.id === 'tsk_101') as Task
const pending = releases.find((release) => release.status === 'pending') as ReleaseCandidate
const published = releases.find((release) => release.is_published) as ReleaseCandidate

describe('canAct', () => {
  it('разрешает владельцу отменить свою задачу', () => {
    expect(canAct(owner, developing, 'cancel')).toEqual({ allowed: true })
  })

  it('запрещает решения по чужой задаче, в том числе администратору', () => {
    expect(canAct(observer, developing, 'cancel').allowed).toBe(false)
    expect(canAct(admin, developing, 'cancel').allowed).toBe(false)
  })

  it('запрещает решение, недопустимое в статусе задачи', () => {
    expect(canAct(owner, developing, 'approve_spec')).toEqual({
      allowed: false,
      reason: 'Недоступно в статусе «Разработка».',
    })
  })

  it('разрешает подтверждение выпуска и откат только администратору', () => {
    expect(canAct(admin, pending, 'approve_release')).toEqual({ allowed: true })
    expect(canAct(admin, published, 'rollback')).toEqual({ allowed: true })
    expect(canAct(owner, pending, 'approve_release').allowed).toBe(false)
    expect(canAct(owner, published, 'rollback').allowed).toBe(false)
  })

  it('запрещает откат неопубликованного кандидата', () => {
    expect(canAct(admin, pending, 'rollback').allowed).toBe(false)
  })
})
