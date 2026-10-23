import { SLOT_STATUSES, type Task } from '@/api/types'

export function countSlots(tasks: readonly Pick<Task, 'status'>[]): number {
  return tasks.filter((task) => SLOT_STATUSES.includes(task.status)).length
}
