import { useId, useState } from 'react'
import { decideRelease, type Run } from '@/api/client'
import type {
  Participant,
  ReleaseCandidate,
  ReleaseDecision,
  ReleaseQueueState,
  ReleaseStatus,
  VerifyResult,
} from '@/api/types'
import { ConfirmButton, DecisionMessage } from '@/components/office/DecisionControls'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import {
  DECISION_LABELS,
  RELEASE_ORIGIN_LABELS,
  RELEASE_STATUS_LABELS,
  formatDateTime,
} from '@/lib/labels'
import { RELEASE_ADMIN_ONLY, canAct } from '@/lib/permissions'
import { newDecision, useDecision } from '@/lib/use-decision'

const QUEUE_STATUSES: readonly ReleaseStatus[] = ['pending', 'deploying', 'verifying', 'rolling_back']
const FIX_SHA = /^[0-9a-f]{40}$/
const REASON_LIMIT = 500

interface ReleaseQueueProps {
  releases: ReleaseCandidate[]
  queue: ReleaseQueueState | null
  participant: Participant
  run: Run
  onRefresh: (release: ReleaseCandidate) => void
}

interface ReleaseItemProps {
  release: ReleaseCandidate
  queue: ReleaseQueueState | null
  participant: Participant
  run: Run
  onRefresh: (release: ReleaseCandidate) => void
}

function useReleaseDecision(release: ReleaseCandidate, run: Run, onRefresh: (release: ReleaseCandidate) => void) {
  return useDecision<ReleaseDecision>(
    (body) => run(() => decideRelease(release.id, body)),
    () => onRefresh(release),
  )
}

function verifyText(verify: VerifyResult): string {
  if (verify.ok) return `пройдена (попыток: ${verify.attempts})`
  const fault = verify.fault ? 'управляемый сбой OFFICE_VERIFY_FAULT; ' : ''
  return `не пройдена: ${fault}${verify.error ?? 'нет данных'}`
}

function ReleaseItem({ release, queue, participant, run, onRefresh }: ReleaseItemProps) {
  const decision = useReleaseDecision(release, run, onRefresh)
  const reasonId = useId()
  const [reason, setReason] = useState('')
  const action: ReleaseDecision = QUEUE_STATUSES.includes(release.status) ? 'resume_releases' : 'rollback'
  const start = () => {
    const body = newDecision(action, release.version)
    const text = reason.trim()
    void decision.submit(action === 'rollback' && text ? { ...body, reason: text } : body)
  }
  const active = queue?.active_release_id === release.id

  return (
    <li className="grid gap-2 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{release.id}</span>
        <Badge variant={release.status === 'failed' ? 'destructive' : 'secondary'}>
          {RELEASE_STATUS_LABELS[release.status]}
        </Badge>
        {release.kind === 'rollback' && <Badge variant="outline">Откат</Badge>}
        {release.origin !== 'task' && release.kind === 'release' && (
          <Badge variant="outline">{RELEASE_ORIGIN_LABELS[release.origin]}</Badge>
        )}
        {release.is_published && <Badge variant="outline">Опубликован сейчас</Badge>}
        {active && <Badge variant="outline">Выполняется</Badge>}
        <span className="text-muted-foreground">Задача {release.task_id}</span>
      </div>
      <dl className="grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-[auto_minmax(0,1fr)]">
        <dt className="text-muted-foreground">Commit</dt>
        <dd className="font-mono break-all">{release.commit_sha}</dd>
        <dt className="text-muted-foreground">Предыдущая версия</dt>
        <dd className="font-mono break-all">{release.expected_previous_sha}</dd>
        <dt className="text-muted-foreground">Проверки CI</dt>
        <dd className="font-mono">{release.ci_run_ids.join(', ') || 'нет'}</dd>
        <dt className="text-muted-foreground">Создан</dt>
        <dd>
          <time dateTime={release.created_at}>{formatDateTime(release.created_at)}</time>
        </dd>
        {release.superseded_by && (
          <>
            <dt className="text-muted-foreground">В составе</dt>
            <dd className="font-mono">{release.superseded_by}</dd>
          </>
        )}
        {release.deploy && (
          <>
            <dt className="text-muted-foreground">Деплой Render</dt>
            <dd>
              <span className="font-mono">{release.deploy.render_status}</span>
              {release.deploy.finished_at && (
                <>
                  {', завершён '}
                  <time dateTime={release.deploy.finished_at}>
                    {formatDateTime(release.deploy.finished_at)}
                  </time>
                </>
              )}
              {release.deploy.error && (
                <span className="text-destructive">, {release.deploy.error}</span>
              )}
            </dd>
          </>
        )}
        {release.verify && (
          <>
            <dt className="text-muted-foreground">Проверка после деплоя</dt>
            <dd className={release.verify.ok ? undefined : 'text-destructive'}>{verifyText(release.verify)}</dd>
          </>
        )}
        {release.rollback && (
          <>
            <dt className="text-muted-foreground">Откат</dt>
            <dd>
              {'к '}
              <span className="font-mono">{release.rollback.target_sha.slice(0, 7)}</span>
              {`, Render: ${release.rollback.render_status ?? 'запрос'}`}
              {release.rollback.verify && `, проверка ${verifyText(release.rollback.verify)}`}
            </dd>
          </>
        )}
        {release.blocked_reason && (
          <>
            <dt className="text-muted-foreground">Причина остановки</dt>
            <dd className="text-destructive">{release.blocked_reason}</dd>
          </>
        )}
        {release.reason && (
          <>
            <dt className="text-muted-foreground">Причина отката</dt>
            <dd>{release.reason}</dd>
          </>
        )}
      </dl>
      {canAct(participant, release, action, queue).allowed && (
        <div className="grid gap-2">
          {action === 'rollback' && (
            <>
              <Label htmlFor={reasonId}>Причина отката (необязательно, до {REASON_LIMIT} символов)</Label>
              <Textarea
                id={reasonId}
                className="max-w-md"
                maxLength={REASON_LIMIT}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </>
          )}
          <div>
            <ConfirmButton
              label={DECISION_LABELS[action]}
              title={action === 'rollback' ? `Откатить выпуск ${release.id}?` : `Возобновить выпуск ${release.id}?`}
              description={
                action === 'rollback'
                  ? `В очередь встанет кандидат отката на версию ${release.expected_previous_sha}.`
                  : 'Офис сверит Render и опубликованную версию и освободит очередь. Если деплой ещё идёт, решение будет отклонено.'
              }
              disabled={decision.pending}
              onConfirm={start}
            />
          </div>
        </div>
      )}
      <DecisionMessage
        notice={decision.notice}
        pending={decision.pending}
        onRetry={(body) => void decision.submit(body)}
      />
    </li>
  )
}

interface BlockedProps {
  blocked: ReleaseCandidate | undefined
  queue: ReleaseQueueState
  participant: Participant
  run: Run
  onRefresh: (release: ReleaseCandidate) => void
}

function ResumeForm({ release, run, onRefresh }: { release: ReleaseCandidate; run: Run; onRefresh: BlockedProps['onRefresh'] }) {
  const id = useId()
  const [fixSha, setFixSha] = useState('')
  const decision = useReleaseDecision(release, run, onRefresh)
  const valid = FIX_SHA.test(fixSha)
  const revert = release.revert
  const retry = revert?.status === 'needs_attention' && !(revert.source === 'auto' && revert.fix_sha)
  return (
    <form
      className="grid gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        if (valid) void decision.submit({ ...newDecision('resume_releases', release.version), fix_sha: fixSha })
      }}
    >
      <Label htmlFor={id}>Исправляющий commit в main (fix_sha, 40 символов)</Label>
      <div className="flex flex-wrap gap-2">
        <Input
          id={id}
          className="max-w-md font-mono"
          value={fixSha}
          onChange={(event) => setFixSha(event.target.value.trim().toLowerCase())}
          aria-invalid={fixSha !== '' && !valid}
        />
        <Button type="submit" disabled={!valid || decision.pending}>
          {DECISION_LABELS.resume_releases}
        </Button>
        {retry && (
          <ConfirmButton
            label="Повторить revert-PR"
            title={`Повторить revert-PR выпуска ${release.id}?`}
            description="Офис закроет прежний revert-PR, если он открыт, заново создаст revert слияний офиса от текущего main и откроет новый PR. Блокировка сохраняется до публикации исправления."
            disabled={decision.pending}
            onConfirm={() => void decision.submit(newDecision('resume_releases', release.version))}
          />
        )}
      </div>
      <DecisionMessage
        notice={decision.notice}
        pending={decision.pending}
        onRetry={(body) => void decision.submit(body)}
      />
    </form>
  )
}

function BlockedNotice({ blocked, queue, participant, run, onRefresh }: BlockedProps) {
  const revert = blocked?.revert
  return (
    <section aria-labelledby="release-blocked-heading" className="grid gap-2 rounded-md border border-destructive p-3 text-sm">
      <h3 id="release-blocked-heading" className="font-medium text-destructive">
        Выпуск заблокирован после отката {queue.release_blocked_by}
      </h3>
      <p>{queue.blocked_reason}</p>
      <p className="text-muted-foreground">
        До публикации исправления деплоятся только откаты и кандидаты, содержащие исправление.
      </p>
      {revert && (
        <p>
          {revert.source === 'auto' ? 'Revert-PR' : 'Исправление владельца'}
          {revert.pr_url && (
            <>
              {': '}
              <a className="tap-target underline" href={revert.pr_url} target="_blank" rel="noreferrer">
                PR #{revert.pr_number}
              </a>
            </>
          )}
          {revert.fix_sha && (
            <>
              {', commit '}
              <span className="font-mono">{revert.fix_sha.slice(0, 7)}</span>
            </>
          )}
          {`, состояние: ${REVERT_LABELS[revert.status]}`}
          {revert.owner_review?.state === 'requested' && revert.owner_review.head_sha === revert.head_sha && (
            <span>. Серверный контур: ждёт одобрения владельца на GitHub</span>
          )}
          {revert.reason && <span className="text-destructive">. {revert.reason}</span>}
        </p>
      )}
      {blocked && canAct(participant, blocked, 'resume_releases', queue).allowed && (
        <ResumeForm release={blocked} run={run} onRefresh={onRefresh} />
      )}
    </section>
  )
}

const REVERT_LABELS: Record<NonNullable<ReleaseCandidate['revert']>['status'], string> = {
  pending: 'ждёт начала',
  active: 'выполняется',
  needs_attention: 'требует внимания',
  merged: 'кандидат исправления создан',
}

export function ReleaseQueue({ releases, queue, participant, run, onRefresh }: ReleaseQueueProps) {
  const pending = releases.filter((release) => QUEUE_STATUSES.includes(release.status))
  const history = releases.filter((release) => !QUEUE_STATUSES.includes(release.status))
  const isAdmin = participant.role === 'admin'
  const item = (release: ReleaseCandidate) => (
    <ReleaseItem
      key={release.id}
      release={release}
      queue={queue}
      participant={participant}
      run={run}
      onRefresh={onRefresh}
    />
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 className="text-lg">Выпуски vibe-chat</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        {queue && !queue.configured && (
          <p role="status" className="text-sm text-destructive">
            Выпуск не настроен: RENDER_API_KEY и RENDER_SERVICE_ID не заданы. Кандидаты ждут в очереди.
          </p>
        )}
        {!isAdmin && <p className="text-sm text-muted-foreground">{RELEASE_ADMIN_ONLY}</p>}
        {queue?.release_blocked_by && (
          <BlockedNotice
            blocked={releases.find((release) => release.id === queue.release_blocked_by)}
            queue={queue}
            participant={participant}
            run={run}
            onRefresh={onRefresh}
          />
        )}
        {queue?.published_sha && (
          <p className="text-sm">
            {'Опубликована версия вне офиса: '}
            <span className="font-mono">{queue.published_sha}</span>
          </p>
        )}
        <section aria-labelledby="release-queue-heading" className="grid gap-2">
          <h3 id="release-queue-heading" className="font-medium">
            Очередь выпуска
          </h3>
          {pending.length === 0 ? (
            <p className="text-sm text-muted-foreground">Очередь пуста.</p>
          ) : (
            <ul className="grid gap-2">{pending.map(item)}</ul>
          )}
        </section>
        <section aria-labelledby="release-history-heading" className="grid gap-2">
          <h3 id="release-history-heading" className="font-medium">
            История выпусков
          </h3>
          {history.length === 0 ? (
            <p className="text-sm text-muted-foreground">Выпусков ещё не было.</p>
          ) : (
            <ul className="grid gap-2">{history.map(item)}</ul>
          )}
        </section>
      </CardContent>
    </Card>
  )
}
