import { useState } from 'react'
import {
  TASK_DECISIONS,
  type CheckReport,
  type Participant,
  type Task,
  type TaskDecision,
  type TaskEvent,
} from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  AGENT_ROLE_LABELS,
  DECISION_LABELS,
  EVENT_TYPE_LABELS,
  PROFILE_LABELS,
  STATUS_LABELS,
  formatDateTime,
} from '@/lib/labels'
import { TASK_DECISION_STATUSES, canAct } from '@/lib/permissions'

interface TaskCardProps {
  task: Task
  participant: Participant
  events: TaskEvent[]
  report: CheckReport | undefined
  diff: string | undefined
}

const panelClass = 'rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50'
const MISSING = 'нет данных'

export function TaskCard({ task, participant, events, report, diff }: TaskCardProps) {
  const [notice, setNotice] = useState<string | null>(null)
  const actions = TASK_DECISIONS.filter((action) =>
    TASK_DECISION_STATUSES[action].includes(task.status),
  )
  const denial = actions
    .map((action) => canAct(participant, task, action))
    .find((permission) => !permission.allowed)

  const decide = (action: TaskDecision) =>
    setNotice(`Тестовый режим: решение «${DECISION_LABELS[action]}» не отправляется на сервер.`)

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 id="task-heading" className="text-lg">
            {task.title}
          </h2>
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-2">
          <Badge variant={task.status === 'rejected' ? 'destructive' : 'secondary'}>
            {STATUS_LABELS[task.status]}
          </Badge>
          <span>Владелец: {task.owner_name}</span>
          <span>Профиль: {PROFILE_LABELS[task.profile]}</span>
          <span>Версия: {task.version}</span>
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {actions.length > 0 && (
          <div className="grid gap-2">
            <div className="flex flex-wrap gap-2">
              {actions.map((action) => (
                <Button
                  key={action}
                  type="button"
                  variant={action === 'cancel' ? 'outline' : 'default'}
                  disabled={!canAct(participant, task, action).allowed}
                  aria-describedby={denial ? 'task-action-reason' : undefined}
                  onClick={() => decide(action)}
                >
                  {DECISION_LABELS[action]}
                </Button>
              ))}
            </div>
            {denial && !denial.allowed && (
              <p id="task-action-reason" className="text-sm text-muted-foreground">
                {denial.reason}
              </p>
            )}
          </div>
        )}
        {notice && (
          <p role="status" className="text-sm text-muted-foreground">
            {notice}
          </p>
        )}
        <Tabs defaultValue="spec">
          <TabsList className="flex h-auto w-full flex-wrap">
            <TabsTrigger value="spec">Требования</TabsTrigger>
            <TabsTrigger value="events">Ход работы</TabsTrigger>
            <TabsTrigger value="diff">Изменения</TabsTrigger>
            <TabsTrigger value="checks">Проверки</TabsTrigger>
            <TabsTrigger value="usage">Расход</TabsTrigger>
          </TabsList>

          <TabsContent value="spec" className={panelClass}>
            <SpecPanel task={task} />
          </TabsContent>

          <TabsContent value="events" className={panelClass}>
            {events.length === 0 ? (
              <p className="text-sm text-muted-foreground">Событий пока нет.</p>
            ) : (
              <ol className="grid gap-2">
                {events.map((event) => (
                  <li key={event.event_id} className="grid gap-1 rounded-md border p-3 text-sm">
                    <span className="flex flex-wrap gap-x-3 gap-y-1">
                      <span className="text-muted-foreground">
                        <time dateTime={event.timestamp}>{formatDateTime(event.timestamp)}</time>
                      </span>
                      <span className="font-medium">{EVENT_TYPE_LABELS[event.type]}</span>
                      {event.role && <span>{AGENT_ROLE_LABELS[event.role]}</span>}
                      {event.tool && <code className="font-mono">{event.tool}</code>}
                      {event.artifact && <code className="font-mono">{event.artifact}</code>}
                    </span>
                    <span className="font-mono text-xs break-all text-muted-foreground">
                      {Object.entries(event.payload)
                        .map(([key, value]) => `${key}: ${String(value)}`)
                        .join('; ')}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </TabsContent>

          <TabsContent value="diff" className={`grid gap-3 ${panelClass}`}>
            {task.changed_files.length === 0 ? (
              <p className="text-sm text-muted-foreground">Изменений нет.</p>
            ) : (
              <>
                <ul className="grid gap-1 font-mono text-sm">
                  {task.changed_files.map((file) => (
                    <li key={file}>{file}</li>
                  ))}
                </ul>
                {diff && (
                  <pre
                    tabIndex={0}
                    aria-label="Unified diff"
                    className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs"
                  >
                    {diff}
                  </pre>
                )}
              </>
            )}
          </TabsContent>

          <TabsContent value="checks" className={`grid gap-3 ${panelClass}`}>
            {!report || report.checks.length === 0 ? (
              <p className="text-sm text-muted-foreground">Проверки ещё не запускались.</p>
            ) : (
              <>
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                  <dt className="text-muted-foreground">Commit</dt>
                  <dd className="font-mono break-all">{report.commit_sha ?? MISSING}</dd>
                  <dt className="text-muted-foreground">Хеш требований</dt>
                  <dd className="font-mono">{report.spec_hash ?? MISSING}</dd>
                  <dt className="text-muted-foreground">Хеш проверок</dt>
                  <dd className="font-mono">{report.suite_hash ?? MISSING}</dd>
                </dl>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <caption className="sr-only">Результаты проверок</caption>
                    <thead className="text-muted-foreground">
                      <tr>
                        <th scope="col" className="py-1 pr-3 font-medium">Команда</th>
                        <th scope="col" className="py-1 pr-3 font-medium">Exit code</th>
                        <th scope="col" className="py-1 pr-3 font-medium">Длительность</th>
                        <th scope="col" className="py-1 font-medium">Артефакты</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.checks.map((check) => (
                        <tr key={check.command} className="border-t align-top">
                          <td className="py-2 pr-3 font-mono">{check.command}</td>
                          <td className="py-2 pr-3">
                            <Badge variant={check.exit_code === 0 ? 'secondary' : 'destructive'}>
                              {check.exit_code}
                            </Badge>
                          </td>
                          <td className="py-2 pr-3">{(check.duration_ms / 1000).toFixed(1)} с</td>
                          <td className="py-2 font-mono text-xs">
                            {check.artifact_urls.map((url) => (
                              <div key={url}>{url.split('/').pop()}</div>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </TabsContent>

          <TabsContent value="usage" className={panelClass}>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-muted-foreground">Обращения к моделям</dt>
              <dd>
                {task.usage.model_calls} из {task.usage.model_calls_limit}
              </dd>
              <dt className="text-muted-foreground">Входные токены</dt>
              <dd>{task.usage.input_tokens.toLocaleString('ru-RU')}</dd>
              <dt className="text-muted-foreground">Выходные токены</dt>
              <dd>{task.usage.output_tokens.toLocaleString('ru-RU')}</dd>
              <dt className="text-muted-foreground">Циклы исправления</dt>
              <dd>{task.fix_cycles} из 2</dd>
            </dl>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  )
}

function SpecPanel({ task }: { task: Task }) {
  if (task.rejection_reason) {
    return (
      <div className="grid gap-2 text-sm">
        <p className="font-medium text-destructive">Задача отклонена фильтром приёма</p>
        <p>{task.rejection_reason}</p>
        <p className="text-muted-foreground">Текст задачи: {task.text}</p>
      </div>
    )
  }
  if (!task.spec) {
    return <p className="text-sm text-muted-foreground">Требования ещё не подготовлены.</p>
  }
  return (
    <div className="grid gap-3 text-sm">
      <p className="text-muted-foreground">
        Версия требований {task.spec.version}, <code className="font-mono">{task.spec.spec_hash}</code>
      </p>
      <p>{task.spec.summary}</p>
      <div className="grid gap-1">
        <h3 className="font-medium">Критерии приёмки</h3>
        <ul className="list-disc pl-5">
          {task.spec.acceptance_criteria.map((criterion) => (
            <li key={criterion}>{criterion}</li>
          ))}
        </ul>
      </div>
      {task.spec.sources.length > 0 && (
        <div className="grid gap-1">
          <h3 className="font-medium">Источники</h3>
          <ul className="list-disc pl-5">
            {task.spec.sources.map((source) => (
              <li key={source.url}>
                <a
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  className="underline underline-offset-4"
                >
                  {source.title}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
