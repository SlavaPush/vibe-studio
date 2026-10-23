import type { ApiError } from '@/api/types'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

export function UpdateNotice() {
  return (
    <div
      role="status"
      className="flex items-center justify-center gap-3 border-b bg-secondary px-4 py-2 text-sm font-medium text-secondary-foreground"
    >
      <span>Доступна новая версия.</span>
      <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
        Обновить страницу
      </Button>
    </div>
  )
}

export function LoadingStatus({ label }: { label: string }) {
  return (
    <p
      role="status"
      className="m-auto animate-in fade-in fill-mode-both delay-300 duration-300 text-sm text-muted-foreground motion-reduce:animate-none"
    >
      {label}
    </p>
  )
}

export function SessionErrorNotice({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Alert variant="destructive" className="w-full max-w-sm">
        <AlertTitle>Не удалось проверить сеанс</AlertTitle>
        <AlertDescription>
          <p>{error.message}</p>
          <Button size="sm" variant="outline" className="mt-2" onClick={onRetry}>
            Повторить
          </Button>
        </AlertDescription>
      </Alert>
    </main>
  )
}

export function RunErrorNotice({ error }: { error: ApiError }) {
  const cancelled = error.code === 'run_cancelled'
  return (
    <Alert variant={cancelled ? 'default' : 'destructive'} className="mx-4 w-auto">
      <AlertTitle>{cancelled ? 'Запрос отменён' : 'Не удалось получить ответ'}</AlertTitle>
      <AlertDescription>
        <p>{error.message}</p>
        {error.retryable && <p>Запрос можно повторить позже.</p>}
        <p className="text-xs">Код операции: {error.operation_id}</p>
      </AlertDescription>
    </Alert>
  )
}
