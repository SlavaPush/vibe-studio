import { useLayoutEffect, useMemo, useRef, useState, type Ref } from 'react'
import type { PullRequestCheck, TaskEvent } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { AGENT_ROLE_LABELS, CHECK_STATUS_LABELS, eventTypeLabel, formatDateTime } from '@/lib/labels'
import {
  STAGE_STATE_LABELS,
  describeEvent,
  describeEventDetails,
  handoffText,
  type AgentKey,
  type AgentState,
  type AgentView,
  type Handoff,
  type HandoffKey,
  type Phrase,
  type Pipeline,
  type Stage,
  type StageState,
} from '@/lib/pipeline'

const linkClass = 'tap-target w-fit underline underline-offset-4'

function PhraseView({ phrase }: { phrase: Phrase }) {
  return (
    <>
      {phrase.text}
      {phrase.code && (
        <code className="font-mono" title={phrase.title}>
          {phrase.code}
        </code>
      )}
      {phrase.tail}
    </>
  )
}

function agentVariant(state: AgentState): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (state === 'working') return 'default'
  if (state === 'done' || state === 'blocked') return 'secondary'
  if (state === 'error' || state === 'stopped') return 'destructive'
  return 'outline'
}

function stageVariant(state: StageState): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (state === 'running') return 'default'
  if (state === 'done') return 'secondary'
  if (state === 'error') return 'destructive'
  return 'outline'
}

function modelText(agent: AgentView): string | null {
  if (agent.state === 'skipped') return null
  if (!agent.model) {
    return agent.state === 'waiting' || agent.state === 'working'
      ? 'Модель появится при первом обращении'
      : 'Модель: нет данных'
  }
  const name = agent.model.provider ? `${agent.model.provider}/${agent.model.model}` : agent.model.model
  return `Модель: ${name}${agent.model.effort ? `, усилие ${agent.model.effort}` : ''}`
}

function AgentBlock({ agent }: { agent: AgentView }) {
  const working = agent.state === 'working'
  const tone = working
    ? 'border-primary ring-2 ring-primary'
    : agent.state === 'error' || agent.state === 'stopped'
      ? 'border-destructive'
      : agent.state === 'skipped'
        ? 'border-dashed text-muted-foreground'
        : ''
  const model = modelText(agent)
  return (
    <div className={`grid gap-1 rounded-md border p-3 text-sm ${tone}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium">{agent.label}</h4>
        <Badge variant={agentVariant(agent.state)}>{agent.stateLabel}</Badge>
      </div>
      {model && <p className="text-xs text-muted-foreground">{model}</p>}
      {working && agent.activity && (
        <p>
          <PhraseView phrase={agent.activity} />
        </p>
      )}
      {agent.note && <p className="text-muted-foreground">{agent.note}</p>}
    </div>
  )
}

function Arrow({ handoff, direction = 'down' }: { handoff: Handoff | undefined; direction?: 'down' | 'up' }) {
  if (!handoff) {
    return <div aria-hidden="true" className="ml-6 h-4 w-px border-l border-dashed" />
  }
  return (
    <div className="flex items-center gap-2 py-1 pl-6 text-xs text-muted-foreground">
      <span aria-hidden="true" className={`relative h-8 w-px ${handoff.active ? 'bg-primary' : 'bg-border'}`}>
        {handoff.active && (
          <span
            className={`absolute left-1/2 size-2 -translate-x-1/2 rounded-full bg-primary motion-reduce:animate-none ${
              direction === 'up' ? 'animate-handoff [animation-direction:reverse]' : 'animate-handoff'
            }`}
          />
        )}
      </span>
      <span aria-hidden="true" className={handoff.active ? 'font-medium text-foreground' : undefined}>
        {direction === 'up' ? '↑' : '↓'} {handoff.label}
        {handoff.key === 'requirements' && handoff.from[0] === 'supervisor' ? ' от Supervisor' : ''}
        {handoff.key === 'findings' ? ' от QA и критика' : ''}
        {handoff.active ? ' · передаётся' : ''}
      </span>
      <span className="sr-only">{handoffText(handoff)}</span>
    </div>
  )
}

function ChecksList({ checks, label }: { checks: PullRequestCheck[]; label: string }) {
  if (checks.length === 0) return null
  return (
    <ul aria-label={label} className="grid gap-1">
      {checks.map((check) => (
        <li key={check.name} className="flex flex-wrap items-center gap-2">
          <code className="font-mono">{check.name}</code>
          <span className="text-muted-foreground">
            {check.status === 'completed'
              ? (check.conclusion ?? 'нет данных')
              : (CHECK_STATUS_LABELS[check.status] ?? check.status)}
          </span>
          {check.url && (
            <a
              href={check.url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Запуск проверки ${check.name}`}
              className={linkClass}
            >
              Запуск
            </a>
          )}
        </li>
      ))}
    </ul>
  )
}

function StageRow({ stage }: { stage: Stage }) {
  return (
    <li className="grid gap-1 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-medium">{stage.title}</h4>
        <Badge variant={stageVariant(stage.state)}>{STAGE_STATE_LABELS[stage.state]}</Badge>
      </div>
      {stage.detail && (
        <p>
          <PhraseView phrase={stage.detail} />
        </p>
      )}
      {stage.link && (
        <a href={stage.link.href} target="_blank" rel="noopener noreferrer" className={linkClass}>
          {stage.link.label}
        </a>
      )}
      <ChecksList checks={stage.checks} label={`${stage.title}: проверки`} />
      {stage.results.length > 0 && (
        <ul aria-label={`${stage.title}: результаты`} className="grid gap-1">
          {stage.results.map((line, index) => (
            <li key={`${index}:${line.name}`}>
              <code className="font-mono">{line.name}</code>: {line.ok ? 'успешно' : 'ошибка'}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function StageBlock({ id, title, stages, empty }: { id: string; title: string; stages: Stage[]; empty?: string }) {
  return (
    <section aria-labelledby={id} className="grid content-start gap-2">
      <h3 id={id} className="font-medium">
        {title}
      </h3>
      {empty ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ol aria-label={`Этапы: ${title}`} className="grid gap-2">
          {stages.map((stage) => (
            <StageRow key={stage.key} stage={stage} />
          ))}
        </ol>
      )}
    </section>
  )
}

export function PipelineBoard({ pipeline }: { pipeline: Pipeline }) {
  const { agents } = pipeline
  const handoff = (key: HandoffKey) => pipeline.handoffs.find((item) => item.key === key)
  const agent = (key: AgentKey) => <AgentBlock agent={agents[key]} />
  return (
    <div className="grid gap-4 desk:grid-cols-2">
      <section aria-labelledby="pipeline-agents-heading" className="grid content-start gap-2">
        <h3 id="pipeline-agents-heading" className="font-medium">
          Агенты
        </h3>
        {pipeline.rework && (
          <p className="text-sm">
            Доработка: попытка {pipeline.rework.attempt} из {pipeline.rework.limit}
          </p>
        )}
        <ol aria-label="Схема конвейера" className="grid">
          <li>{agent('intake')}</li>
          <li>
            <Arrow handoff={handoff('task')} />
            {agent('supervisor')}
          </li>
          <li>
            <Arrow handoff={handoff('questions')} />
            {agent('researcher')}
          </li>
          <li>
            <Arrow handoff={handoff('requirements')} />
            {handoff('findings') && <Arrow handoff={handoff('findings')} direction="up" />}
            {agent('developer')}
          </li>
          <li>
            <Arrow handoff={handoff('code')} />
            <p className="pb-1 text-xs text-muted-foreground">QA и критик работают параллельно</p>
            <ul aria-label="Параллельные проверки" className="grid gap-2">
              <li>{agent('qa')}</li>
              <li>{agent('critic')}</li>
            </ul>
          </li>
          <li>
            <Arrow handoff={handoff('reports')} />
            {agent('pr_reviewer')}
          </li>
        </ol>
      </section>
      <div className="grid content-start gap-4">
        <StageBlock id="pipeline-github-heading" title="GitHub" stages={pipeline.github} />
        <StageBlock
          id="pipeline-release-heading"
          title="Выпуск"
          stages={pipeline.release}
          empty={pipeline.releaseStarted ? undefined : 'Выпуск начнётся после слияния PR.'}
        />
      </div>
    </div>
  )
}

const AT_TOP = 4

function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export function EventFeed({ events }: { events: readonly TaskEvent[] }) {
  const scroller = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLOListElement>(null)
  const ordered = useMemo(() => [...events].reverse(), [events])
  const newest = useRef(ordered[0]?.seq ?? 0)
  const [unseen, setUnseen] = useState(0)

  useLayoutEffect(() => {
    const previous = newest.current
    newest.current = ordered[0]?.seq ?? 0
    const node = scroller.current
    if (!node || newest.current <= previous || node.scrollTop <= AT_TOP) return
    const added = ordered.filter((event) => event.seq > previous).length
    const items = list.current?.children
    const anchor = items?.[added] as HTMLElement | undefined
    const first = items?.[0] as HTMLElement | undefined
    if (anchor && first) node.scrollTop += anchor.offsetTop - first.offsetTop
    setUnseen((count) => count + added)
  }, [ordered])

  const toTop = () => {
    const node = scroller.current
    if (!node) return
    if (typeof node.scrollTo === 'function') node.scrollTo({ top: 0, behavior: reducedMotion() ? 'auto' : 'smooth' })
    else node.scrollTop = 0
    setUnseen(0)
  }

  return (
    <div className="grid gap-2">
      <h3 id="task-feed-heading" className="font-medium">
        Лента событий
      </h3>
      <div className="relative">
        {unseen > 0 && (
          <Button
            type="button"
            size="sm"
            className="absolute top-2 left-1/2 z-10 -translate-x-1/2 shadow-md"
            onClick={toTop}
          >
            Новые события ({unseen})
          </Button>
        )}
        <div
          ref={scroller}
          role="region"
          aria-labelledby="task-feed-heading"
          tabIndex={0}
          onScroll={(event) => {
            if (unseen > 0 && event.currentTarget.scrollTop <= AT_TOP) setUnseen(0)
          }}
          className="relative max-h-[60vh] overflow-y-auto overscroll-contain rounded-md [overflow-anchor:none] desk:max-h-96"
        >
          <EventList ref={list} events={ordered} />
        </div>
      </div>
    </div>
  )
}

function EventDetails({ event }: { event: TaskEvent }) {
  const lines = describeEventDetails(event)
  if (lines.length === 0) return null
  return (
    <dl className="mt-1 grid gap-x-3 gap-y-1 desk:grid-cols-[max-content_minmax(0,1fr)]">
      {lines.map((line, index) => (
        <div key={`${index}:${line.label}`} className="grid gap-x-3 desk:col-span-2 desk:grid-cols-subgrid">
          <dt className="text-muted-foreground">{line.label}</dt>
          <dd className="break-words whitespace-pre-wrap">
            {line.href ? (
              <a href={line.href} target="_blank" rel="noopener noreferrer" className={linkClass}>
                {line.value}
              </a>
            ) : (
              line.value
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function EventList({ events, ref }: { events: readonly TaskEvent[]; ref: Ref<HTMLOListElement> }) {
  return (
    <ol ref={ref} className="grid gap-2" aria-label="События задачи">
      {events.map((event) => (
        <li key={event.event_id} data-seq={event.seq} className="grid gap-1 rounded-md border p-3 text-sm">
          <span className="flex flex-wrap gap-x-3 gap-y-1">
            <span className="text-muted-foreground">
              <time dateTime={event.timestamp}>{formatDateTime(event.timestamp)}</time>
            </span>
            {event.role && (
              <span className="font-medium">
                {(AGENT_ROLE_LABELS as Record<string, string | undefined>)[event.role] ?? event.role}
              </span>
            )}
            <span>
              <PhraseView phrase={describeEvent(event)} />
            </span>
          </span>
          <details className="text-xs">
            <summary className="min-h-11 w-fit cursor-pointer content-center text-muted-foreground desk:min-h-0">
              Подробности
            </summary>
            <EventDetails event={event} />
            <p className="mt-1 text-muted-foreground">
              {eventTypeLabel(event.type)} (<code className="font-mono">{event.type}</code>)
            </p>
            <details className="mt-1">
              <summary className="min-h-11 w-fit cursor-pointer content-center text-muted-foreground desk:min-h-0">
                Данные
              </summary>
              <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted p-2 font-mono break-all whitespace-pre-wrap">
                {JSON.stringify({ tool: event.tool, artifact: event.artifact, payload: event.payload }, null, 2)}
              </pre>
            </details>
          </details>
        </li>
      ))}
    </ol>
  )
}
