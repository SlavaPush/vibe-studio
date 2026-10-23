import type { ReleaseCandidate, Slots, Task } from '@/api/types'
import type { StreamState } from '@/lib/events'

export interface OfficeState {
  tasks: Task[]
  releases: ReleaseCandidate[]
  slots: Slots | null
  slotsSeq: number
  selectedId: string | null
  stream: StreamState
}

export type OfficeAction =
  | { type: 'reset' }
  | { type: 'tasks'; tasks: Task[] }
  | { type: 'releases'; releases: ReleaseCandidate[] }
  | { type: 'slots'; seq: number; slots: Slots }
  | { type: 'select'; id: string }
  | { type: 'stream'; stream: StreamState }

export const initialOfficeState: OfficeState = {
  tasks: [],
  releases: [],
  slots: null,
  slotsSeq: 0,
  selectedId: null,
  stream: 'connecting',
}

interface Versioned {
  id: string
  version: number
  created_at: string
}

export function mergeByVersion<T extends Versioned>(current: readonly T[], incoming: readonly T[]): T[] {
  const byId = new Map(current.map((item) => [item.id, item]))
  for (const item of incoming) {
    const known = byId.get(item.id)
    if (!known || item.version >= known.version) byId.set(item.id, item)
  }
  return [...byId.values()].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id),
  )
}

export function officeReducer(state: OfficeState, action: OfficeAction): OfficeState {
  switch (action.type) {
    case 'reset':
      return initialOfficeState
    case 'tasks': {
      const tasks = mergeByVersion(state.tasks, action.tasks)
      return { ...state, tasks, selectedId: state.selectedId ?? tasks[0]?.id ?? null }
    }
    case 'releases':
      return { ...state, releases: mergeByVersion(state.releases, action.releases) }
    case 'slots':
      return action.seq > state.slotsSeq
        ? { ...state, slots: action.slots, slotsSeq: action.seq }
        : state
    case 'select':
      return { ...state, selectedId: action.id }
    case 'stream':
      return { ...state, stream: action.stream }
  }
}
