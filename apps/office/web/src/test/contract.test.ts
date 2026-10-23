import { describe, expect, it } from 'vitest'
import {
  AGENT_ROLES,
  MODEL_PROFILES,
  RELEASE_KINDS,
  RELEASE_STATUSES,
  TASK_EVENT_TYPES,
  TASK_STATUSES,
} from '@/api/types'
import examples from '@/mocks/contract-examples.json'
import { events, releases, tasks } from '@/mocks/data'

describe('типы панели и contracts/api.md', () => {
  it('перечень статусов совпадает с контрактом', () => {
    expect(TASK_STATUSES).toEqual(examples['TaskStatus[]'][0])
  })

  it('перечень типов событий совпадает с контрактом', () => {
    expect(TASK_EVENT_TYPES).toEqual(examples['TaskEventType[]'][0])
  })

  it('тестовые данные используют значения перечислений', () => {
    for (const task of tasks) {
      expect(TASK_STATUSES).toContain(task.status)
      expect(MODEL_PROFILES).toContain(task.profile)
    }
    for (const event of events) {
      expect(TASK_EVENT_TYPES).toContain(event.type)
      if (event.role) expect(AGENT_ROLES).toContain(event.role)
    }
    for (const release of releases) {
      expect(RELEASE_STATUSES).toContain(release.status)
      expect(RELEASE_KINDS).toContain(release.kind)
    }
  })

  it('тестовые данные содержат три задачи в разных статусах', () => {
    expect(new Set(tasks.map((task) => task.status)).size).toBe(3)
  })
})
