import { useEffect, useState, type FormEvent } from 'react'
import { ApiRequestError, NETWORK_MESSAGE, login, me } from '@/api/client'
import type { Participant } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export const COOKIE_MISSING = 'Сеанс не создан, проверьте настройки браузера: панель должна быть открыта по http://localhost:5173, cookie должны быть разрешены.'

interface AccessGateProps {
  notice: string | null
  onLogin: (participant: Participant) => void
}

function describe(error: unknown): { text: string; pause: number } {
  if (!(error instanceof ApiRequestError)) return { text: NETWORK_MESSAGE, pause: 0 }
  if (error.status === 429) {
    const seconds = error.retryAfter ?? 60
    return { text: `${error.message} Повторить можно через ${seconds} с.`, pause: seconds }
  }
  return { text: error.message, pause: 0 }
}

export function AccessGate({ notice, onLogin }: AccessGateProps) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [pause, setPause] = useState(0)
  const message = error ?? notice

  useEffect(() => {
    if (pause <= 0) return
    const timer = setTimeout(() => setPause(0), pause * 1000)
    return () => clearTimeout(timer)
  }, [pause])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending || pause > 0) return
    const displayName = name.trim()
    if (!code.trim()) {
      setError('Введите код доступа офиса.')
      return
    }
    if (!displayName || displayName.length > 40) {
      setError('Укажите имя длиной от 1 до 40 символов.')
      return
    }
    setPending(true)
    setError(null)
    try {
      await login({ access_code: code.trim(), display_name: displayName })
    } catch (failure) {
      const { text, pause: seconds } = describe(failure)
      setError(text)
      setPause(seconds)
      setPending(false)
      return
    }
    try {
      onLogin(await me())
    } catch (failure) {
      setError(
        failure instanceof ApiRequestError && failure.status === 401
          ? COOKIE_MISSING
          : describe(failure).text,
      )
      setPending(false)
    }
  }

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Вход в панель офиса</h1>
          </CardTitle>
          <CardDescription>Введите код доступа офиса и имя, которое увидят другие участники.</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={submit} aria-busy={pending} noValidate>
            <div className="grid gap-2">
              <Label htmlFor="access-code">Код доступа</Label>
              <Input
                id="access-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                autoComplete="off"
                aria-invalid={error !== null}
                aria-describedby={message ? 'access-error' : undefined}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="display-name">Отображаемое имя</Label>
              <Input
                id="display-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoComplete="nickname"
                maxLength={40}
                aria-invalid={error !== null}
                aria-describedby={message ? 'access-error' : undefined}
                required
              />
            </div>
            {message && (
              <p id="access-error" role="alert" className="text-sm text-destructive">
                {message}
              </p>
            )}
            <Button type="submit" disabled={pending || pause > 0}>
              {pending ? 'Входим…' : 'Войти'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
