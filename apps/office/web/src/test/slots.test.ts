import { describe, expect, it } from 'vitest'
import { TASK_STATUSES } from '@/api/types'
import { countSlots } from '@/lib/slots'
import { tasks } from '@/mocks/data'

const OCCUPYING = ['awaiting_spec_approval', 'developing', 'reviewing']

describe('countSlots', () => {
  it.each(TASK_STATUSES)('считает статус %s', (status) => {
    expect(countSlots([{ status }])).toBe(OCCUPYING.includes(status) ? 1 : 0)
  })

  it('считает занятые слоты тестовых задач', () => {
    expect(countSlots(tasks)).toBe(1)
  })
})
