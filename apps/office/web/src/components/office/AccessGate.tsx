import { useEffect, useState, type FormEvent } from 'react'
import { ApiRequestError, NETWORK_MESSAGE, login, me } from '@/api/client'
import type { Participant } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export const COOKIE_MISSING = 'Сеанс не создан, проверьте настройки браузера: панель должна быть открыта по http://localhost:5173, cookie должны быть разрешены.'
export const CHECK_AGAIN = 'Проверить сеанс снова'

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
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [pause, setPause] = useState(0)
  const [loggedIn, setLoggedIn] = useState(false)
  const message = error ?? notice

  useEffect(() => {
    if (pause <= 0) return
    const timer = setTimeout(() => setPause(0), pause * 1000)
    return () => clearTimeout(timer)
  }, [pause])

  const verify = async (missingText = COOKIE_MISSING) => {
    setPending(true)
    try {
      onLogin(await me())
      return
    } catch (failure) {
      const missing = failure instanceof ApiRequestError && failure.status === 401
      setError(
        missing
          ? missingText
          : `${describe(failure).text} Код повторно не отправляется: проверьте сеанс снова.`,
      )
      setLoggedIn(!missing)
    }
    setPending(false)
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending || pause > 0 || loggedIn) return
    if (!code.trim()) {
      setError('Введите личный код участника.')
      return
    }
    setPending(true)
    setError(null)
    try {
      await login({ access_code: code.trim() })
    } catch (failure) {
      const { text, pause: seconds } = describe(failure)
      if (!(failure instanceof ApiRequestError) || failure.status === 0) {
        await verify(text)
        return
      }
      setError(text)
      setPause(seconds)
      setPending(false)
      return
    }
    await verify()
  }

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Вход в панель офиса</h1>
          </CardTitle>
          <CardDescription>
            Введите личный код участника. Это тот же код, что и для чата. Вход по коду одноразовый: имя и роль
            берутся из кода.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={submit} aria-busy={pending} noValidate>
            <div className="grid gap-2">
              <Label htmlFor="access-code">Личный код участника</Label>
              <Input
                id="access-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                autoComplete="off"
                disabled={loggedIn}
                aria-invalid={error !== null && !loggedIn}
                aria-describedby={message ? 'access-error' : undefined}
                required
              />
            </div>
            {message && (
              <p id="access-error" role="alert" className="text-sm text-destructive">
                {message}
              </p>
            )}
            {loggedIn ? (
              <Button type="button" disabled={pending} onClick={() => void verify()}>
                {pending ? 'Проверяем…' : CHECK_AGAIN}
              </Button>
            ) : (
              <Button type="submit" disabled={pending || pause > 0}>
                {pending ? 'Входим…' : 'Войти'}
              </Button>
            )}
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
