import { useEffect, useMemo, useState } from 'react'
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
  type PullRequestReview,
  type ReleaseCandidate,
  type Task,
  type TaskDecision,
  type TaskEvent,
  type TaskStatus,
} from '@/api/types'
import { ConfirmButton, DecisionMessage } from '@/components/office/DecisionControls'
import { EventFeed, PipelineBoard } from '@/components/office/PipelineBoard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { openTaskEvents, type StreamState } from '@/lib/events'
import {
  CHECKLIST_LABELS,
  CHECK_STATUS_LABELS,
  CONTOUR_LABELS,
  DECISION_LABELS,
  REVIEW_VERDICT_LABELS,
  STATUS_LABELS,
  failureLabel,
  formatDateTime,
} from '@/lib/labels'
import { TASK_DECISION_STATUSES, canAct } from '@/lib/permissions'
import { buildPipeline } from '@/lib/pipeline'
import { newDecision, useDecision } from '@/lib/use-decision'

interface TaskCardProps {
  task: Task
  participant: Participant
  run: Run
  onRefresh: () => void
  lastEventId?: number
  releases?: ReleaseCandidate[]
}

const ACTIVE_STATUSES: readonly TaskStatus[] = [
  'developing',
  'reviewing',
  'awaiting_integration',
  'awaiting_release',
  'deploying',
]

const NO_RELEASES: ReleaseCandidate[] = []

function isActive(task: Task): boolean {
  return ACTIVE_STATUSES.includes(task.status) || (task.status === 'awaiting_spec_approval' && task.spec === null)
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

export function TaskCard({
  task,
  participant,
  run,
  onRefresh,
  lastEventId = 0,
  releases = NO_RELEASES,
}: TaskCardProps) {
  const [tab, setTab] = useState('spec')
  const [seen, setSeen] = useState(lastEventId)
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
  const pipeline = useMemo(
    () => (tab === 'events' && events.length > 0 ? buildPipeline(events, task, releases) : null),
    [tab, events, task, releases],
  )
  const fresh = tab !== 'events' && lastEventId > seen
  const changeTab = (value: string) => {
    if (value === 'events' || tab === 'events') setSeen(lastEventId)
    setTab(value)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 id="task-heading" tabIndex={-1} className="text-lg">
            {task.title}
          </h2>
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-2">
          <Badge variant={task.status === 'rejected' ? 'destructive' : 'secondary'}>
            {STATUS_LABELS[task.status]}
          </Badge>
          {task.contour && <Badge variant="outline">Контур: {CONTOUR_LABELS[task.contour]}</Badge>}
          <span>Владелец: {task.owner_name}</span>
          <span>Версия: {task.version}</span>
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-4">
        {task.status === 'failed' && task.failure_kind && (
          <p className="text-sm text-destructive">Причина ошибки: {failureLabel(task)}</p>
        )}
        {(task.rework_count > 0 || task.rework_reason) && <ReworkPanel task={task} />}
        {(task.pr_url || task.checks || task.merge_sha) && <PullRequestPanel task={task} />}
        {task.pr_review && <ReviewPanel review={task.pr_review} />}
        {task.owner_review && <OwnerReviewPanel task={task} />}
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
        <Tabs value={tab} onValueChange={changeTab}>
          <TabsList className="flex h-auto w-full flex-wrap">
            <TabsTrigger value="spec">Требования</TabsTrigger>
            <TabsTrigger value="events">
              Ход работы
              {fresh && (
                <>
                  <span
                    aria-hidden="true"
                    className={`size-2 rounded-full bg-destructive ${
                      isActive(task) ? 'animate-pulse motion-reduce:animate-none' : ''
                    }`}
                  />
                  <span className="sr-only">, есть новые события</span>
                </>
              )}
            </TabsTrigger>
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
            {pipeline === null ? (
              journal === 'open' && <p className="text-sm text-muted-foreground">Событий пока нет.</p>
            ) : (
              <>
                <PipelineBoard pipeline={pipeline} />
                <h3 className="font-medium">Лента событий</h3>
                <EventFeed events={events} />
              </>
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
            <dl className="grid grid-cols-1 gap-x-3 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
              <dt className="text-muted-foreground">Обращения к моделям</dt>
              <dd>
                {task.usage.model_calls} из {task.usage.model_calls_limit}
              </dd>
              <dt className="text-muted-foreground">Оценка стоимости</dt>
              <dd>${task.usage.cost_usd.toFixed(4)}</dd>
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

function commitUrl(task: Task, sha: string): string | null {
  if (!task.pr_url || !/\/pull\/\d+$/.test(task.pr_url)) return null
  return task.pr_url.replace(/\/pull\/\d+$/, `/commit/${sha}`)
}

function ReworkPanel({ task }: { task: Task }) {
  return (
    <section aria-labelledby="task-rework-heading" className="grid gap-1 text-sm">
      <h3 id="task-rework-heading" className="font-medium">
        {task.rework_count > 0
          ? `Доработка после отката: попытка ${task.rework_count} из ${task.rework_limit}`
          : 'Выпуск задачи остановлен'}
      </h3>
      {task.rework_reason && <p>Причина: {task.rework_reason}</p>}
    </section>
  )
}

function PullRequestPanel({ task }: { task: Task }) {
  const mergeUrl = task.merge_sha ? commitUrl(task, task.merge_sha) : null
  const mergeChecks = task.checks !== null && task.checks.head_sha === task.merge_sha
  const checksLabel = mergeChecks ? 'Обязательные проверки итогового commit' : 'Обязательные проверки PR'
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
          className="tap-target w-fit underline underline-offset-4"
        >
          PR #{task.pr_number}
        </a>
      ) : (
        <p className="text-muted-foreground">PR ещё не открыт.</p>
      )}
      {task.merge_sha && (
        <p>
          PR слит в main, итоговый commit{' '}
          {mergeUrl ? (
            <a
              href={mergeUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Итоговый commit ${task.merge_sha.slice(0, 7)}`}
              className="tap-target font-mono underline underline-offset-4"
            >
              {task.merge_sha.slice(0, 7)}
            </a>
          ) : (
            <code className="font-mono">{task.merge_sha.slice(0, 7)}</code>
          )}
        </p>
      )}
      {task.checks && (
        <div className="grid gap-1">
          <p className="text-muted-foreground">
            {checksLabel} для <code className="font-mono">{task.checks.head_sha.slice(0, 7)}</code>
          </p>
          <ul aria-label={checksLabel} className="grid gap-1">
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
                    className="tap-target underline underline-offset-4"
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

function ReviewPanel({ review }: { review: PullRequestReview }) {
  return (
    <section aria-labelledby="task-review-heading" className="grid gap-2 text-sm">
      <h3 id="task-review-heading" className="flex flex-wrap items-center gap-2 font-medium">
        Решение агента ревью PR
        <Badge variant={review.verdict === 'approve' ? 'secondary' : 'destructive'}>
          {REVIEW_VERDICT_LABELS[review.verdict]}
        </Badge>
      </h3>
      <p className="text-muted-foreground">
        Для head <code className="font-mono">{review.head_sha.slice(0, 7)}</code>,{' '}
        <time dateTime={review.created_at}>{formatDateTime(review.created_at)}</time>
      </p>
      <p>{review.reasons}</p>
      {review.owner_summary && (
        <div className="grid gap-1">
          <h4 className="font-medium">Сводка для владельца</h4>
          <p className="whitespace-pre-wrap">{review.owner_summary}</p>
        </div>
      )}
      {review.required_changes.length > 0 && (
        <div className="grid gap-1">
          <h4 className="font-medium">Что изменить</h4>
          <ul aria-label="Что изменить" className="list-disc pl-5">
            {review.required_changes.map((change) => (
              <li key={change}>{change}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="grid gap-1">
        <h4 className="font-medium">Чек-лист</h4>
        <ul aria-label="Чек-лист агента ревью" className="grid gap-1">
          {review.checklist.map((item) => (
            <li key={item.item} className="flex flex-wrap items-center gap-2">
              <Badge variant={item.ok ? 'secondary' : 'destructive'}>{item.ok ? 'В порядке' : 'Замечание'}</Badge>
              <span>{CHECKLIST_LABELS[item.item] ?? item.item}</span>
              {item.note && <span className="text-muted-foreground">{item.note}</span>}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

function OwnerReviewPanel({ task }: { task: Task }) {
  const owner = task.owner_review
  if (!owner || owner.head_sha !== task.head_sha) return null
  const link = task.pr_url && (
    <a href={task.pr_url} target="_blank" rel="noreferrer" className="underline">
      PR #{task.pr_number}
    </a>
  )
  return (
    <section aria-labelledby="task-owner-heading" className="grid gap-1 text-sm">
      <h3 id="task-owner-heading" className="font-medium">
        Одобрение владельца
      </h3>
      {owner.state === 'requested' && (
        <p role="status">
          Ждёт одобрения владельца на GitHub: {link}. PR серверного контура сливается только после одобрения.
        </p>
      )}
      {owner.state === 'approved' && (
        <p>
          Владелец {owner.login} одобрил {link} для head{' '}
          <code className="font-mono">{owner.head_sha.slice(0, 7)}</code>.
        </p>
      )}
      {owner.state === 'changes_requested' && (
        <>
          <p className="text-destructive">
            Владелец {owner.login} отклонил {link}, PR закрыт.
          </p>
          <p>Комментарий владельца: {owner.comment || 'не указан'}</p>
        </>
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
      <dl className="grid grid-cols-1 gap-x-3 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
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
                        className="tap-target underline underline-offset-4"
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
      {task.spec.plan.length > 0 && (
        <div className="grid gap-1">
          <h3 className="font-medium">План реализации</h3>
          <ol className="list-decimal pl-5">
            {task.spec.plan.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ol>
        </div>
      )}
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
                  className="tap-target underline underline-offset-4"
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
