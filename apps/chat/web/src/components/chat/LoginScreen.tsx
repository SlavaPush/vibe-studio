import { useState, type FormEvent } from 'react'
import { ApiRequestError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface LoginScreenProps {
  onSubmit: (code: string) => Promise<void>
}

export function LoginScreen({ onSubmit }: LoginScreenProps) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!code.trim()) {
      setError('Введите код доступа.')
      return
    }
    setPending(true)
    try {
      await onSubmit(code.trim())
    } catch (reason) {
      setError(reason instanceof ApiRequestError ? reason.error.message : 'Не удалось войти. Повторите позже.')
      setPending(false)
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            <h1>Вход в vibe-chat</h1>
          </CardTitle>
          <CardDescription>Введите код доступа от организатора.</CardDescription>
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
            <Button type="submit" disabled={pending}>
              Войти
            </Button>
          </form>
          <section aria-labelledby="access-info-title" className="mt-6 flex flex-col gap-2 text-sm text-muted-foreground">
            <h2 id="access-info-title" className="font-medium text-foreground">
              Как работает доступ
            </h2>
            <p>
              Это демонстрационный проект. Вместо регистрации и паролей здесь используются одноразовые коды доступа. Так
              ограничивается круг участников и расход на запросы к моделям.
            </p>
            <p>Код выдаёт организатор. Код действует для одного входа.</p>
            <p>
              История диалогов хранится в этом браузере. После удаления cookie, закрытия окна инкогнито или на другом
              устройстве нужен новый код. Прежняя история будет недоступна.
            </p>
          </section>
        </CardContent>
      </Card>
    </main>
  )
}
