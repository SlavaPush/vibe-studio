import { describe, expect, it } from 'vitest'
import type { Participant, ReleaseCandidate, Task } from '@/api/types'
import { canAct } from '@/lib/permissions'
import { blockedRelease, participants, releaseQueue, releases, tasks } from '@/mocks/data'

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

  it('разрешает откат и возобновление выпуска только администратору', () => {
    expect(canAct(admin, published, 'rollback')).toEqual({ allowed: true })
    expect(canAct(owner, published, 'rollback').allowed).toBe(false)
    expect(canAct(owner, blockedRelease, 'resume_releases', releaseQueue)).toEqual({
      allowed: false,
      reason: 'Откат и возобновление выпуска доступны только администратору.',
    })
  })

  it('возобновление доступно для блокировки и остановленного кандидата', () => {
    expect(canAct(admin, blockedRelease, 'resume_releases', releaseQueue)).toEqual({ allowed: true })
    expect(canAct(admin, pending, 'resume_releases', releaseQueue).allowed).toBe(false)
    const halted = { ...pending, blocked_reason: 'Состав выпуска не подтверждён.' }
    expect(canAct(admin, halted, 'resume_releases', releaseQueue)).toEqual({ allowed: true })
    expect(
      canAct(admin, halted, 'resume_releases', { ...releaseQueue, configured: false }).allowed,
    ).toBe(false)
    const active = { ...pending, status: 'deploying' as const }
    const running = { ...releaseQueue, active_release_id: active.id }
    expect(canAct(admin, active, 'resume_releases', running).allowed).toBe(false)
    expect(
      canAct(admin, { ...active, blocked_reason: 'Исход запроса деплоя не установлен.' }, 'resume_releases', running),
    ).toEqual({ allowed: true })
  })

  it('запрещает откат неопубликованного кандидата', () => {
    expect(canAct(admin, pending, 'rollback').allowed).toBe(false)
  })

  it('разрешает утвердить требования только после их подготовки', () => {
    const preparing: Task = { ...developing, status: 'awaiting_spec_approval', spec: null }
    expect(canAct(owner, preparing, 'approve_spec')).toEqual({
      allowed: false,
      reason: 'Требования ещё не подготовлены.',
    })
    expect(canAct(owner, { ...preparing, spec: developing.spec }, 'approve_spec')).toEqual({
      allowed: true,
    })
  })

  it('запрещает повтор задачи с непройденными проверками PR или отклонённым PR', () => {
    const failed: Task = { ...developing, status: 'failed', failure_kind: 'checks_failed' }
    expect(canAct(owner, failed, 'retry')).toEqual({
      allowed: false,
      reason:
        'Обязательные проверки PR не пройдены, PR закрыт. Повтор недоступен: поставьте новую задачу.',
    })
    expect(canAct(owner, { ...failed, failure_kind: 'pr_rejected' }, 'retry').allowed).toBe(false)
    expect(canAct(owner, { ...failed, status: 'needs_attention' }, 'retry')).toEqual({
      allowed: true,
    })
  })

  it('разрешает отмену задачи с открытым PR', () => {
    const integrating: Task = { ...developing, status: 'awaiting_integration', pr_number: 3 }
    expect(canAct(owner, integrating, 'cancel')).toEqual({ allowed: true })
  })

  it('запрещает отмену после слияния PR', () => {
    const merged: Task = {
      ...developing,
      status: 'needs_attention',
      pr_number: 3,
      merge_sha: 'c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6',
    }
    expect(canAct(owner, merged, 'cancel')).toEqual({
      allowed: false,
      reason: 'PR задачи уже слит в main: отмена недоступна.',
    })
  })

  it('запрещает отмену во время слияния с неустановленным исходом', () => {
    for (const status of ['awaiting_integration', 'needs_attention'] as const) {
      const merging: Task = { ...developing, status, pr_number: 3, merge_pending: true }
      expect(canAct(owner, merging, 'cancel')).toEqual({
        allowed: false,
        reason: 'Выполняется слияние PR задачи: отмена недоступна.',
      })
    }
  })

  it('на этапе интеграции не проверяет бюджет графа для повтора', () => {
    const usage = { ...developing.usage, model_calls: 30 }
    const pushing: Task = {
      ...developing,
      status: 'needs_attention',
      head_sha: null,
      pr_number: null,
      merge_sha: null,
      integration_started: true,
      usage,
    }
    expect(canAct(owner, pushing, 'retry')).toEqual({ allowed: true })
    expect(canAct(owner, { ...pushing, integration_started: false }, 'retry').allowed).toBe(false)
  })

  it('разрешает администратору повтор чужой задачи только после начала интеграции', () => {
    const halted: Task = { ...developing, status: 'needs_attention', integration_started: true }
    expect(canAct(admin, halted, 'retry')).toEqual({ allowed: true })
    expect(canAct(observer, halted, 'retry').allowed).toBe(false)
    expect(canAct(admin, halted, 'cancel').allowed).toBe(false)
    expect(canAct(admin, { ...halted, integration_started: false }, 'retry').allowed).toBe(false)
  })

  it('запрещает повтор при исчерпанном бюджете обращений', () => {
    const failed: Task = { ...developing, status: 'failed' }
    expect(canAct(owner, failed, 'retry')).toEqual({ allowed: true })
    const exhausted: Task = { ...failed, usage: { ...failed.usage, model_calls: 30 } }
    expect(canAct(owner, exhausted, 'retry')).toEqual({
      allowed: false,
      reason: 'Бюджет обращений к моделям исчерпан. Повтор недоступен.',
    })
  })
})
