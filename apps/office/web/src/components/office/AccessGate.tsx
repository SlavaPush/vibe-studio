import { useState, type FormEvent } from 'react'
import type { Participant } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ACCESS_CODES } from '@/mocks/data'

export function AccessGate({ onLogin }: { onLogin: (participant: Participant) => void }) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const displayName = name.trim()
    const role = ACCESS_CODES[code.trim()]
    if (!displayName || displayName.length > 40) {
      setError('Укажите имя длиной от 1 до 40 символов.')
      return
    }
    if (!role) {
      setError('Неверный код доступа офиса.')
      return
    }
    onLogin({
      id: 'prt_session',
      display_name: displayName,
      role,
      expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
    })
  }

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Вход в панель офиса</h1>
          </CardTitle>
          <CardDescription>
            Тестовый режим: код участника <code>OFFICE-2026</code>, код администратора{' '}
            <code>ADMIN-2026</code>.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4" onSubmit={submit} noValidate>
            <div className="grid gap-2">
              <Label htmlFor="access-code">Код доступа</Label>
              <Input
                id="access-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                autoComplete="off"
                aria-invalid={error !== null}
                aria-describedby={error ? 'access-error' : undefined}
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
                aria-describedby={error ? 'access-error' : undefined}
                required
              />
            </div>
            {error && (
              <p id="access-error" role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button type="submit">Войти</Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
