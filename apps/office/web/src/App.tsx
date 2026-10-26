import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import {
  ApiRequestError,
  STALE_MESSAGE,
  asApiError,
  getSlots,
  getReleaseQueue,
  getTask,
  isStale,
  isUnauthorized,
  listReleases,
  listTasks,
  logout,
  me,
  type Run,
} from '@/api/client'
import {
  RELEASE_DECISIONS,
  type Participant,
  type ReleaseCandidate,
  type Task,
  type TaskEvent,
} from '@/api/types'
import { AccessGate, CHECK_AGAIN } from '@/components/office/AccessGate'
import { ReleaseQueue } from '@/components/office/ReleaseQueue'
import { TaskCard } from '@/components/office/TaskCard'
import { TaskForm } from '@/components/office/TaskForm'
import { TaskStack } from '@/components/office/TaskStack'
import { ThemeToggle } from '@/components/office/ThemeToggle'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { AboutDialog } from '@/components/office/AboutDialog'
import { connectOfficeEvents, type OfficeEvents, type StreamState } from '@/lib/events'
import { initialOfficeState, officeReducer } from '@/lib/office-state'
import { nextPulse, type TaskPulse } from '@/lib/pipeline'
import { clearAwaitedDecisions } from '@/lib/use-decision'

const COALESCE_MS = 300
const SESSION_EXPIRED = 'Сеанс истёк или код отозван. Для нового входа нужен новый личный код.'

const STREAM_LABELS: Record<StreamState, string> = {
  connecting: 'Подключение к обновлениям…',
  open: 'Обновления подключены',
  reconnecting: 'Переподключение к обновлениям…',
}

const RELEASE_EVENTS = new Set<TaskEvent['type']>([
  'release_candidate',
  'deploy_status',
  'rollback_status',
  'release_blocked',
  'release_unblocked',
  'external_publish',
])

function touchesReleases(event: TaskEvent): boolean {
  if (RELEASE_EVENTS.has(event.type)) return true
  if ('release_id' in event.payload || 'revert_of' in event.payload) return true
  return (
    event.type === 'decision_recorded' &&
    (RELEASE_DECISIONS as readonly unknown[]).includes(event.payload.decision)
  )
}

function narrow(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 1023.98px)').matches
}

function App() {
  const [participant, setParticipant] = useState<Participant | null>(null)
  const [checking, setChecking] = useState(true)
  const [gateNotice, setGateNotice] = useState<string | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [state, dispatch] = useReducer(officeReducer, initialOfficeState)
  const [leaving, setLeaving] = useState(false)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [headingFocus, setHeadingFocus] = useState(0)
  const [pulses, setPulses] = useState<Record<string, TaskPulse>>({})
  const generation = useRef(0)
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const slotsSeq = useRef(0)
  const stream = useRef<OfficeEvents | null>(null)

  useEffect(() => {
    if (headingFocus > 0) document.getElementById('task-heading')?.focus()
  }, [headingFocus])

  useEffect(() => {
    if (!checking) return
    let active = true
    me().then(
      (current) => {
        if (!active) return
        setSessionError(null)
        setParticipant(current)
      },
      (error: unknown) => {
        if (active) setSessionError(isUnauthorized(error) ? null : asApiError(error).message)
      },
    ).finally(() => {
      if (active) setChecking(false)
    })
    return () => {
      active = false
    }
  }, [checking])

  const endSession = useCallback((notice: string | null) => {
    generation.current += 1
    for (const timer of timers.current.values()) clearTimeout(timer)
    timers.current.clear()
    clearAwaitedDecisions()
    dispatch({ type: 'reset' })
    setPulses({})
    setParticipant(null)
    setGateNotice(notice)
    setLeaving(false)
    setLogoutError(null)
  }, [])

  const expire = useCallback(() => endSession(SESSION_EXPIRED), [endSession])

  const run: Run = useCallback(
    async <T,>(operation: () => Promise<T>): Promise<T> => {
      const started = generation.current
      try {
        const result = await operation()
        if (started !== generation.current) throw new ApiRequestError(0, 'stale', STALE_MESSAGE)
        return result
      } catch (error) {
        if (started !== generation.current) throw new ApiRequestError(0, 'stale', STALE_MESSAGE)
        if (isUnauthorized(error)) expire()
        throw error
      }
    },
    [expire],
  )

  const load = useCallback(
    <T,>(operation: () => Promise<T>, apply: (value: T) => void): Promise<void> => {
      const started = generation.current
      return run(operation).then((value) => {
        if (started === generation.current) apply(value)
      })
    },
    [run],
  )

  const refresh = useCallback((job: () => Promise<unknown>) => {
    job().catch((error: unknown) => {
      if (!isStale(error) && !isUnauthorized(error)) stream.current?.recover()
    })
  }, [])

  const loadTask = useCallback(
    (id: string) => load(() => getTask(id), (task) => dispatch({ type: 'tasks', tasks: [task] })),
    [load],
  )

  const loadReleases = useCallback(
    () =>
      Promise.all([
        load(listReleases, (releases) => dispatch({ type: 'releases', releases })),
        load(getReleaseQueue, (queue) => dispatch({ type: 'queue', queue })),
      ]),
    [load],
  )

  const loadSlots = useCallback(() => {
    slotsSeq.current += 1
    const seq = slotsSeq.current
    return load(getSlots, (slots) => dispatch({ type: 'slots', seq, slots }))
  }, [load])

  const resync = useCallback(
    () =>
      Promise.all([
        load(listTasks, (tasks) => dispatch({ type: 'tasks', tasks })),
        loadReleases(),
        loadSlots(),
      ]),
    [load, loadReleases, loadSlots],
  )

  const schedule = useCallback(
    (key: string, job: () => Promise<unknown>) => {
      if (timers.current.has(key)) return
      timers.current.set(
        key,
        setTimeout(() => {
          timers.current.delete(key)
          refresh(job)
        }, COALESCE_MS),
      )
    },
    [refresh],
  )

  const onEvent = useCallback(
    (event: TaskEvent) => {
      setPulses((current) => {
        const previous = current[event.task_id]
        const next = nextPulse(previous, event)
        return next === previous ? current : { ...current, [event.task_id]: next }
      })
      schedule(`task:${event.task_id}`, () => loadTask(event.task_id))
      if (event.type === 'status_changed') schedule('slots', loadSlots)
      if (touchesReleases(event)) schedule('releases', loadReleases)
    },
    [schedule, loadTask, loadSlots, loadReleases],
  )

  useEffect(() => {
    if (!participant) return
    const events = connectOfficeEvents({
      resync,
      onEvent,
      onState: (state) => dispatch({ type: 'stream', stream: state }),
    })
    stream.current = events
    const pending = timers.current
    return () => {
      events.stop()
      if (stream.current === events) stream.current = null
      for (const timer of pending.values()) clearTimeout(timer)
      pending.clear()
    }
  }, [participant, resync, onEvent])

  if (checking) {
    return (
      <main className="flex min-h-svh items-center justify-center p-4">
        <p role="status" className="text-sm text-muted-foreground">
          Проверяем сеанс…
        </p>
      </main>
    )
  }

  if (!participant && sessionError) {
    return (
      <main className="flex min-h-svh items-center justify-center p-4">
        <div className="grid max-w-sm gap-4">
          <h1 className="text-xl font-semibold">Панель офиса</h1>
          <p role="alert" className="text-sm text-destructive">
            Не удалось проверить сеанс. {sessionError}
          </p>
          <Button type="button" onClick={() => setChecking(true)}>
            {CHECK_AGAIN}
          </Button>
        </div>
      </main>
    )
  }

  if (!participant) {
    return (
      <AccessGate
        notice={gateNotice}
        onLogin={(current) => {
          setGateNotice(null)
          setParticipant(current)
        }}
      />
    )
  }

  const task = state.tasks.find((item) => item.id === state.selectedId)
  const created = (item: Task) => {
    dispatch({ type: 'tasks', tasks: [item] })
    if (item.status !== 'rejected') dispatch({ type: 'select', id: item.id })
  }
  const select = (id: string, focus: boolean) => {
    dispatch({ type: 'select', id })
    if (focus) setHeadingFocus((count) => count + 1)
  }
  const releaseChanged = (release: ReleaseCandidate) => {
    refresh(loadReleases)
    refresh(() => loadTask(release.task_id))
  }
  const leave = async () => {
    if (leaving) return
    setLeaving(true)
    setLogoutError(null)
    try {
      await run(logout)
    } catch (error) {
      if (!isStale(error) && !isUnauthorized(error)) {
        setLogoutError(asApiError(error).message)
        setLeaving(false)
      }
      return
    }
    endSession(null)
  }

  return (
    <div className="min-h-svh">
      <header className="border-b">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <h1 className="text-xl font-semibold">Панель офиса</h1>
          <p role="status" className="text-sm text-muted-foreground">
            {STREAM_LABELS[state.stream]}
          </p>
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <p className="text-sm">
              {participant.display_name} ({participant.role === 'admin' ? 'администратор' : 'участник'})
            </p>
            <AboutDialog />
            <ThemeToggle />
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button type="button" variant="outline" disabled={leaving}>
                  {leaving ? 'Выходим…' : 'Выйти'}
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Выйти из панели?</AlertDialogTitle>
                  <AlertDialogDescription>
                    Вход в офис по личному коду одноразовый. После выхода войти снова по этому коду невозможно, для
                    нового входа нужен новый код.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Остаться</AlertDialogCancel>
                  <AlertDialogAction onClick={() => void leave()}>Выйти</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
          {logoutError && (
            <p role="alert" className="w-full text-sm text-destructive">
              {logoutError}
            </p>
          )}
        </div>
      </header>
      <main className="mx-auto grid max-w-7xl grid-cols-1 gap-6 px-4 py-6">
        <TaskForm run={run} onCreated={created} />
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <TaskStack
            tasks={state.tasks}
            slots={state.slots}
            selectedId={state.selectedId}
            pulses={pulses}
            onSelect={(id) => select(id, narrow())}
          />
          {task && (
            <TaskCard
              key={task.id}
              task={task}
              participant={participant}
              run={run}
              onRefresh={() => refresh(() => loadTask(task.id))}
              lastEventId={pulses[task.id]?.eventId ?? 0}
              releases={state.releases.filter((release) => release.task_id === task.id)}
            />
          )}
        </div>
        <ReleaseQueue
          releases={state.releases}
          tasks={state.tasks}
          queue={state.queue}
          participant={participant}
          run={run}
          onRefresh={releaseChanged}
          onOpenTask={(id) => select(id, true)}
        />
      </main>
    </div>
  )
}

export default App
