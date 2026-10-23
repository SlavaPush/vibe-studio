import type { ApiError } from '@/api/types'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

export function UpdateNotice() {
  return (
    <div role="status" className="flex items-center justify-center gap-3 border-b bg-muted px-4 py-2 text-sm">
      <span>Доступна новая версия.</span>
      <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
        Обновить страницу
      </Button>
    </div>
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
