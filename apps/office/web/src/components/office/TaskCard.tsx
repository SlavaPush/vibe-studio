import { useEffect, useState } from 'react'
import {
  asApiError,
  decideTask,
  isStale,
  me,
  taskChecks,
  taskDiff,
  type ApiRequestError,
  type Run,
} from '@/api/client'
import {
  TASK_DECISIONS,
  type CheckReport,
  type Participant,
  type PullRequestCheck,
  type Task,
  type TaskDecision,
  type TaskEvent,
} from '@/api/types'
import { ConfirmButton, DecisionMessage } from '@/components/office/DecisionControls'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { openTaskEvents, type StreamState } from '@/lib/events'
import {
  AGENT_ROLE_LABELS,
  CHECK_STATUS_LABELS,
  DECISION_LABELS,
  EVENT_TYPE_LABELS,
  FAILURE_KIND_LABELS,
  PROFILE_LABELS,
  STATUS_LABELS,
  formatDateTime,
} from '@/lib/labels'
import { TASK_DECISION_STATUSES, canAct } from '@/lib/permissions'
import { newDecision, useDecision } from '@/lib/use-decision'

interface TaskCardProps {
  task: Task
  participant: Participant
  run: Run
  onRefresh: () => void
}

type Loadable<T> =
  | { state: 'loading' }
  | { state: 'ready'; value: T }
  | { state: 'error'; error: ApiRequestError }

const panelClass = 'rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50'
const MISSING = 'нет данных'
const LOADING = 'Загрузка…'

const JOURNAL_LABELS: Record<StreamState, string> = {
  connecting: 'Подключение к журналу…',
  open: '',
  reconnecting: 'Журнал переподключается…',
}

function failed<T>(error: unknown): Loadable<T> | null {
  if (isStale(error)) return null
  return { state: 'error', error: asApiError(error) }
}

export function TaskCard({ task, participant, run, onRefresh }: TaskCardProps) {
  const [tab, setTab] = useState('spec')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [journal, setJournal] = useState<StreamState>('connecting')
  const [diff, setDiff] = useState<Loadable<string>>({ state: 'loading' })
  const [report, setReport] = useState<Loadable<CheckReport>>({ state: 'loading' })
  const decision = useDecision<TaskDecision>((body) => run(() => decideTask(task.id, body)), onRefresh)

  useEffect(() => {
    if (tab !== 'events') return
    return openTaskEvents(task.id, {
      onEvent: (event) =>
        setEvents((current) =>
          current.some((item) => item.event_id === event.event_id)
            ? current
            : [...current, event].sort((a, b) => a.seq - b.seq),
        ),
      onState: setJournal,
      onBroken: () => run(me),
    })
  }, [tab, task.id, run])

  useEffect(() => {
    if (tab !== 'diff') return
    let active = true
    run(() => taskDiff(task.id)).then(
      (value) => active && setDiff({ state: 'ready', value }),
      (error: unknown) => {
        const result = failed<string>(error)
        if (active && result) setDiff(result)
      },
    )
    return () => {
      active = false
    }
  }, [tab, task.id, task.version, run])

  useEffect(() => {
    if (tab !== 'checks') return
    let active = true
    run(() => taskChecks(task.id)).then(
      (value) => active && setReport({ state: 'ready', value }),
      (error: unknown) => {
        const result = failed<CheckReport>(error)
        if (active && result) setReport(result)
      },
    )
    return () => {
      active = false
    }
  }, [tab, task.id, task.version, run])

  const actions = TASK_DECISIONS.filter((action) =>
    TASK_DECISION_STATUSES[action].includes(task.status),
  )
  const denial = actions
    .map((action) => canAct(participant, task, action))
    .find((permission) => !permission.allowed)
  const start = (action: TaskDecision) => void decision.submit(newDecision(action, task.version))

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
        {task.status === 'failed' && task.failure_kind && (
          <p className="text-sm text-destructive">
            Причина ошибки: {FAILURE_KIND_LABELS[task.failure_kind]}
          </p>
        )}
        {(task.pr_url || task.checks) && <PullRequestPanel task={task} />}
        {actions.length > 0 && (
          <div className="grid gap-2">
            <div className="flex flex-wrap gap-2">
              {actions.map((action) => {
                const allowed = canAct(participant, task, action).allowed
                const describedBy = allowed ? undefined : 'task-action-reason'
                const disabled = !allowed || decision.pending
                if (action === 'cancel') {
                  return (
                    <ConfirmButton
                      key={action}
                      label={DECISION_LABELS[action]}
                      title="Отменить задачу?"
                      description={
                        task.pr_url
                          ? 'Отмена необратима: работа над задачей останавливается, офис закрывает PR задачи.'
                          : 'Отмена необратима: работа над задачей останавливается, слот освобождается.'
                      }
                      disabled={disabled}
                      describedBy={describedBy}
                      onConfirm={() => start(action)}
                    />
                  )
                }
                return (
                  <Button
                    key={action}
                    type="button"
                    disabled={disabled}
                    aria-describedby={describedBy}
                    onClick={() => start(action)}
                  >
                    {DECISION_LABELS[action]}
                  </Button>
                )
              })}
            </div>
            {denial && !denial.allowed && (
              <p id="task-action-reason" className="text-sm text-muted-foreground">
                {denial.reason}
              </p>
            )}
          </div>
        )}
        <DecisionMessage
          notice={decision.notice}
          pending={decision.pending}
          onRetry={(body) => void decision.submit(body)}
        />
        <Tabs value={tab} onValueChange={setTab}>
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

          <TabsContent value="events" className={`grid gap-3 ${panelClass}`}>
            <p role="status" className="text-sm text-muted-foreground empty:hidden">
              {JOURNAL_LABELS[journal]}
            </p>
            {events.length === 0 ? (
              journal === 'open' && <p className="text-sm text-muted-foreground">Событий пока нет.</p>
            ) : (
              <ol className="grid gap-2" aria-label="События задачи">
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
            {task.changed_files.length > 0 && (
              <ul className="grid gap-1 font-mono text-sm" aria-label="Изменённые файлы">
                {task.changed_files.map((file) => (
                  <li key={file}>{file}</li>
                ))}
              </ul>
            )}
            <DiffView diff={diff} />
          </TabsContent>

          <TabsContent value="checks" className={`grid gap-3 ${panelClass}`}>
            <ReportView report={report} />
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

function checkVariant(check: PullRequestCheck): 'secondary' | 'destructive' | 'outline' {
  if (check.status !== 'completed') return 'outline'
  return check.conclusion === 'success' ? 'secondary' : 'destructive'
}

function checkLabel(check: PullRequestCheck): string {
  if (check.status === 'completed') return check.conclusion ?? MISSING
  return CHECK_STATUS_LABELS[check.status] ?? check.status
}

function PullRequestPanel({ task }: { task: Task }) {
  return (
    <section aria-labelledby="task-pr-heading" className="grid gap-2 text-sm">
      <h3 id="task-pr-heading" className="font-medium">
        Pull request
      </h3>
      {task.pr_url ? (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          className="w-fit underline underline-offset-4"
        >
          PR #{task.pr_number}
        </a>
      ) : (
        <p className="text-muted-foreground">PR ещё не открыт.</p>
      )}
      {task.checks && (
        <div className="grid gap-1">
          <p className="text-muted-foreground">
            Обязательные проверки для <code className="font-mono">{task.checks.head_sha.slice(0, 7)}</code>
          </p>
          <ul aria-label="Обязательные проверки PR" className="grid gap-1">
            {task.checks.checks.map((check) => (
              <li key={check.name} className="flex flex-wrap items-center gap-2">
                <code className="font-mono">{check.name}</code>
                <Badge variant={checkVariant(check)}>{checkLabel(check)}</Badge>
                {check.url && (
                  <a
                    href={check.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Журнал проверки ${check.name}`}
                    className="underline underline-offset-4"
                  >
                    Журнал
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function DiffView({ diff }: { diff: Loadable<string> }) {
  if (diff.state === 'loading') return <p className="text-sm text-muted-foreground">{LOADING}</p>
  if (diff.state === 'error') {
    const { error } = diff
    if (error.status === 404) {
      return <p className="text-sm text-muted-foreground">Рабочая копия задачи ещё не создана.</p>
    }
    return (
      <p role="alert" className="text-sm text-destructive">
        {error.message}
      </p>
    )
  }
  if (diff.value === '') return <p className="text-sm text-muted-foreground">Изменений нет.</p>
  return (
    <pre
      tabIndex={0}
      aria-label="Unified diff"
      className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs"
    >
      {diff.value}
    </pre>
  )
}

function ReportView({ report }: { report: Loadable<CheckReport> }) {
  if (report.state === 'loading') return <p className="text-sm text-muted-foreground">{LOADING}</p>
  if (report.state === 'error') {
    return (
      <p role="alert" className="text-sm text-destructive">
        {report.error.message}
      </p>
    )
  }
  const { value } = report
  if (value.checks.length === 0) {
    return <p className="text-sm text-muted-foreground">Проверки ещё не запускались.</p>
  }
  return (
    <>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Commit</dt>
        <dd className="font-mono break-all">{value.commit_sha ?? MISSING}</dd>
        <dt className="text-muted-foreground">Хеш требований</dt>
        <dd className="font-mono">{value.spec_hash ?? MISSING}</dd>
        <dt className="text-muted-foreground">Хеш проверок</dt>
        <dd className="font-mono">{value.suite_hash ?? MISSING}</dd>
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
            {value.checks.map((check, index) => (
              <tr key={`${index}:${check.command}`} className="border-t align-top">
                <td className="py-2 pr-3 font-mono">{check.command}</td>
                <td className="py-2 pr-3">
                  <Badge variant={check.exit_code === 0 ? 'secondary' : 'destructive'}>
                    {check.exit_code}
                  </Badge>
                </td>
                <td className="py-2 pr-3">{(check.duration_ms / 1000).toFixed(1)} с</td>
                <td className="py-2 font-mono text-xs">
                  {check.artifact_urls.map((url) => (
                    <div key={url}>
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="underline underline-offset-4"
                      >
                        {url.split('/').pop()}
                      </a>
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
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
    return (
      <div className="grid gap-2 text-sm">
        <p className="text-muted-foreground">Требования ещё не подготовлены.</p>
        <p className="text-muted-foreground">Текст задачи: {task.text}</p>
      </div>
    )
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
