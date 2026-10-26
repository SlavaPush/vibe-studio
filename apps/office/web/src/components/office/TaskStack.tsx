import type { Slots, Task } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { STATUS_LABELS } from '@/lib/labels'
import { cn } from 'cn'

interface TaskStackProps {
  tasks: Task[]
  slots: Slots | null
  selectedId: string | null
  onSelect: (taskId: string) => void
}

export function TaskStack({ tasks, slots, selectedId, onSelect }: TaskStackProps) {
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
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
