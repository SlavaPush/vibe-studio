import { useEffect, useState } from 'react'
import type { Participant } from '@/api/types'
import { AccessGate } from '@/components/office/AccessGate'
import { ReleaseQueue } from '@/components/office/ReleaseQueue'
import { TaskCard } from '@/components/office/TaskCard'
import { TaskStack } from '@/components/office/TaskStack'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { checkReports, diffs, events, participants, releases, tasks } from '@/mocks/data'

function App() {
  const [session, setSession] = useState<Participant | null>(null)
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState(tasks[0]?.id ?? null)
  const [dark, setDark] = useState(false)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
  }, [dark])

  if (!session) {
    return (
      <AccessGate
        onLogin={(participant) => {
          setSession(participant)
          setCurrentId(participant.id)
        }}
      />
    )
  }

  const people = [session, ...participants]
  const participant = people.find((person) => person.id === currentId) ?? session
  const task = tasks.find((item) => item.id === selectedId)

  return (
    <div className="min-h-svh">
      <header className="border-b">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <h1 className="text-xl font-semibold">Панель офиса</h1>
          <Badge variant="outline">Тестовые данные</Badge>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Label htmlFor="participant-select">Тестовый участник</Label>
            <select
              id="participant-select"
              value={participant.id}
              onChange={(event) => setCurrentId(event.target.value)}
              className="h-9 rounded-md border border-input bg-background px-3 text-sm focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {people.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.display_name} ({person.role === 'admin' ? 'администратор' : 'участник'})
                </option>
              ))}
            </select>
            <Button
              type="button"
              variant="outline"
              aria-pressed={dark}
              onClick={() => setDark((value) => !value)}
            >
              Тёмная тема
            </Button>
            <Button type="button" variant="ghost" onClick={() => setSession(null)}>
              Выйти
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto grid max-w-7xl gap-6 px-4 py-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <TaskStack tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} />
          {task && (
            <TaskCard
              key={task.id}
              task={task}
              participant={participant}
              events={events.filter((event) => event.task_id === task.id)}
              report={checkReports.find((report) => report.task_id === task.id)}
              diff={diffs[task.id]}
            />
          )}
        </div>
        <ReleaseQueue releases={releases} participant={participant} />
      </main>
    </div>
  )
}

export default App
