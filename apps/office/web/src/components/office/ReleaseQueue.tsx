import { useState } from 'react'
import type { Participant, ReleaseCandidate, ReleaseDecision, ReleaseStatus } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DECISION_LABELS, RELEASE_STATUS_LABELS, formatDateTime } from '@/lib/labels'
import { canAct } from '@/lib/permissions'

const QUEUE_STATUSES: readonly ReleaseStatus[] = ['pending', 'approved', 'deploying']

interface ReleaseQueueProps {
  releases: ReleaseCandidate[]
  participant: Participant
}

export function ReleaseQueue({ releases, participant }: ReleaseQueueProps) {
  const [notice, setNotice] = useState<string | null>(null)
  const queue = releases.filter((release) => QUEUE_STATUSES.includes(release.status))
  const history = releases.filter((release) => !QUEUE_STATUSES.includes(release.status))
  const isAdmin = participant.role === 'admin'

  const decide = (action: ReleaseDecision, release: ReleaseCandidate) =>
    setNotice(
      `Тестовый режим: решение «${DECISION_LABELS[action]}» для ${release.id} не отправляется на сервер.`,
    )

  const renderRelease = (release: ReleaseCandidate, action: ReleaseDecision) => (
    <li key={release.id} className="grid gap-2 rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{release.id}</span>
        <Badge variant={release.status === 'failed' ? 'destructive' : 'secondary'}>
          {RELEASE_STATUS_LABELS[release.status]}
        </Badge>
        {release.kind === 'rollback' && <Badge variant="outline">Откат</Badge>}
        {release.is_published && <Badge variant="outline">Опубликован сейчас</Badge>}
        <span className="text-muted-foreground">Задача {release.task_id}</span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Commit</dt>
        <dd className="font-mono break-all">{release.commit_sha}</dd>
        <dt className="text-muted-foreground">Предыдущая версия</dt>
        <dd className="font-mono break-all">{release.expected_previous_sha}</dd>
        <dt className="text-muted-foreground">Проверки CI</dt>
        <dd className="font-mono">{release.ci_run_ids.join(', ')}</dd>
        <dt className="text-muted-foreground">Создан</dt>
        <dd>
          <time dateTime={release.created_at}>{formatDateTime(release.created_at)}</time>
        </dd>
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
      </dl>
      {canAct(participant, release, action).allowed && (
        <div>
          <Button
            type="button"
            variant={action === 'rollback' ? 'outline' : 'default'}
            onClick={() => decide(action, release)}
          >
            {DECISION_LABELS[action]}
          </Button>
        </div>
      )}
    </li>
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 className="text-lg">Выпуски vibe-chat</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        {!isAdmin && (
          <p className="text-sm text-muted-foreground">
            Подтверждение выпуска и откат доступны только администратору.
          </p>
        )}
        {notice && (
          <p role="status" className="text-sm text-muted-foreground">
            {notice}
          </p>
        )}
        <section aria-labelledby="release-queue-heading" className="grid gap-2">
          <h3 id="release-queue-heading" className="font-medium">
            Очередь выпуска
          </h3>
          {queue.length === 0 ? (
            <p className="text-sm text-muted-foreground">Очередь пуста.</p>
          ) : (
            <ul className="grid gap-2">
              {queue.map((release) => renderRelease(release, 'approve_release'))}
            </ul>
          )}
        </section>
        <section aria-labelledby="release-history-heading" className="grid gap-2">
          <h3 id="release-history-heading" className="font-medium">
            История выпусков
          </h3>
          {history.length === 0 ? (
            <p className="text-sm text-muted-foreground">Выпусков ещё не было.</p>
          ) : (
            <ul className="grid gap-2">
              {history.map((release) => renderRelease(release, 'rollback'))}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  )
}
