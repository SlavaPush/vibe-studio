import { describe, expect, it } from 'vitest'
import {
  AGENT_ROLES,
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
      expect(task).not.toHaveProperty('profile')
      expect(task.usage.cost_usd).toBeGreaterThanOrEqual(0)
      if (task.spec) expect(Array.isArray(task.spec.plan)).toBe(true)
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

describe('рабочий код панели', () => {
  it('не импортирует тестовые данные', () => {
    const sources = import.meta.glob<string>(['../**/*.{ts,tsx}', '!../test/**', '!../mocks/**'], {
      query: '?raw',
      import: 'default',
      eager: true,
    })
    expect(Object.keys(sources)).toContain('../App.tsx')
    const offenders = Object.entries(sources)
      .filter(([, source]) => /from\s+['"](@\/mocks|\.{1,2}\/(?:\.\.\/)*mocks)/.test(source))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
