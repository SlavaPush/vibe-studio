import { useState, type FormEvent } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface LoginScreenProps {
  onSubmit: (code: string) => void
}

export function LoginScreen({ onSubmit }: LoginScreenProps) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!code.trim()) {
      setError('Введите код доступа.')
      return
    }
    onSubmit(code.trim())
  }

  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Вход в vibe-chat</h1>
          </CardTitle>
          <CardDescription>
            История диалогов сохраняется в этом браузере. После удаления cookie или на другом
            устройстве она будет недоступна.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} noValidate className="flex flex-col gap-3">
            <Label htmlFor="demo-code">Код доступа</Label>
            <Input
              id="demo-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? 'demo-code-error' : undefined}
              autoComplete="off"
            />
            {error && (
              <p id="demo-code-error" role="alert" className="text-sm text-destructive">
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
