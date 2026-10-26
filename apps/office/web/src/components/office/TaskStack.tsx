import type { Slots, Task } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { AGENT_ROLE_LABELS, STATUS_LABELS } from '@/lib/labels'
import { activeRole, formatElapsed, isLive, type TaskPulse } from '@/lib/pipeline'
import { useNow } from '@/lib/use-now'
import { cn } from 'cn'

interface TaskStackProps {
  tasks: Task[]
  slots: Slots | null
  selectedId: string | null
  pulses: Record<string, TaskPulse>
  onSelect: (taskId: string) => void
}

function PulseMark({ pulse, now }: { pulse: TaskPulse | undefined; now: number }) {
  const elapsed = pulse ? formatElapsed(now - Date.parse(pulse.since), false) : null
  const active = pulse ? activeRole(pulse.roles) : null
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
      <span aria-hidden="true" className="size-2 shrink-0 animate-pulse rounded-full bg-primary motion-reduce:animate-none" />
      {elapsed && <span>{active ? `${AGENT_ROLE_LABELS[active.role]} · ${elapsed}` : elapsed}</span>}
    </span>
  )
}

export function TaskStack({ tasks, slots, selectedId, pulses, onSelect }: TaskStackProps) {
  const now = useNow(tasks.some((task) => isLive(task.status) && pulses[task.id]))
  return (
    <section aria-labelledby="tasks-heading" className="grid content-start gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 id="tasks-heading" className="text-lg font-semibold">
          Задачи
        </h2>
        <p className="text-sm text-muted-foreground">
          {slots ? `Занято слотов: ${slots.used} из ${slots.total}` : 'Слоты загружаются…'}
        </p>
      </div>
      {tasks.length === 0 ? (
        <p className="text-sm text-muted-foreground">Задач пока нет.</p>
      ) : (
        <ul className="grid gap-2">
          {tasks.map((task) => (
            <li key={task.id}>
              <button
                type="button"
                onClick={() => onSelect(task.id)}
                aria-current={task.id === selectedId ? 'true' : undefined}
                className={cn(
                  'grid w-full gap-1 rounded-lg border bg-card p-3 text-left text-card-foreground transition-colors hover:bg-accent',
                  task.id === selectedId && 'border-primary',
                )}
              >
                <span className="line-clamp-2 font-medium" title={task.title}>
                  {task.title}
                </span>
                <span className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <Badge variant={task.status === 'rejected' ? 'destructive' : 'secondary'}>
                    {STATUS_LABELS[task.status]}
                  </Badge>
                  <span>Заказчик: {task.owner_name}</span>
                </span>
                {isLive(task.status) && <PulseMark pulse={pulses[task.id]} now={now} />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
