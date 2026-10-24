import { useEffect, useState, type FormEvent } from 'react'
import { asApiError, createTask, isStale, isUnauthorized, type Run } from '@/api/client'
import { MODEL_PROFILES, type ModelProfile, type Task } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { PROFILE_LABELS } from '@/lib/labels'

const TEXT_MIN = 20
const TEXT_MAX = 4000
const LENGTH_MESSAGE = `Текст задачи должен содержать от ${TEXT_MIN} до ${TEXT_MAX} символов.`

interface TaskFormProps {
  run: Run
  onCreated: (task: Task) => void
}

interface Result {
  tone: 'info' | 'error'
  text: string
}

export function TaskForm({ run, onCreated }: TaskFormProps) {
  const [text, setText] = useState('')
  const [profile, setProfile] = useState<ModelProfile>('hybrid')
  const [pending, setPending] = useState(false)
  const [pause, setPause] = useState(0)
  const [result, setResult] = useState<Result | null>(null)
  const length = Array.from(text).length
  const invalid = length > TEXT_MAX || result?.text === LENGTH_MESSAGE

  useEffect(() => {
    if (pause <= 0) return
    const timer = setTimeout(() => setPause(0), pause * 1000)
    return () => clearTimeout(timer)
  }, [pause])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending || pause > 0) return
    if (length < TEXT_MIN || length > TEXT_MAX) {
      setResult({ tone: 'error', text: LENGTH_MESSAGE })
      return
    }
    setPending(true)
    setResult(null)
    try {
      const task = await run(() => createTask({ text, profile }))
      onCreated(task)
      if (task.status === 'rejected') {
        setResult({
          tone: 'error',
          text: `Задача отклонена: ${task.rejection_reason ?? 'причина не указана.'}`,
        })
      } else {
        setText('')
        setResult({ tone: 'info', text: 'Задача принята и ждёт свободного слота.' })
      }
    } catch (error) {
      const failure = asApiError(error)
      if (isStale(failure) || isUnauthorized(failure)) return
      if (failure.status === 429) {
        const seconds = failure.retryAfter ?? 60
        setPause(seconds)
        setResult({ tone: 'error', text: `${failure.message} Повторить можно через ${seconds} с.` })
        return
      }
      setResult({ tone: 'error', text: failure.message })
    } finally {
      setPending(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2 className="text-lg">Новая задача</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form className="grid gap-3" onSubmit={submit} aria-busy={pending} noValidate>
          <div className="grid gap-2">
            <Label htmlFor="task-text">Текст задачи</Label>
            <Textarea
              id="task-text"
              value={text}
              onChange={(event) => setText(event.target.value)}
              rows={4}
              readOnly={pending}
              aria-invalid={invalid}
              aria-describedby="task-text-hint"
              required
            />
            <p id="task-text-hint" className="text-sm text-muted-foreground">
              От {TEXT_MIN} до {TEXT_MAX} символов. Введено: {length}.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid gap-2">
              <Label htmlFor="task-profile">Профиль моделей</Label>
              <select
                id="task-profile"
                value={profile}
                disabled={pending}
                onChange={(event) => setProfile(event.target.value as ModelProfile)}
                className="h-11 rounded-md border border-input bg-background px-3 text-sm focus-visible:ring-[3px] focus-visible:ring-ring/50 desk:h-9"
              >
                {MODEL_PROFILES.map((value) => (
                  <option key={value} value={value}>
                    {PROFILE_LABELS[value]}
                  </option>
                ))}
              </select>
            </div>
            <Button type="submit" disabled={pending || pause > 0}>
              {pending ? 'Проверяем задачу…' : 'Поставить задачу'}
            </Button>
          </div>
          <div role="status" className="text-sm text-muted-foreground">
            {pending && 'Фильтр приёма проверяет задачу. Это занимает до 30 секунд.'}
            {!pending && result?.tone === 'info' && result.text}
          </div>
          {!pending && result?.tone === 'error' && (
            <p role="alert" className="text-sm text-destructive">
              {result.text}
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  )
}
