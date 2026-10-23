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
import type { DecisionBody, DecisionNotice } from '@/lib/use-decision'

interface DecisionMessageProps<D extends string> {
  notice: DecisionNotice<D> | null
  pending: boolean
  onRetry: (body: DecisionBody<D>) => void
}

export function DecisionMessage<D extends string>({ notice, pending, onRetry }: DecisionMessageProps<D>) {
  if (!notice) return null
  const retry = notice.retry
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <p
        role={notice.tone === 'error' ? 'alert' : 'status'}
        className={notice.tone === 'error' ? 'text-destructive' : 'text-muted-foreground'}
      >
        {notice.text}
      </p>
      {retry && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={pending}
          onClick={() => onRetry(retry)}
        >
          Отправить решение повторно
        </Button>
      )}
    </div>
  )
}

interface ConfirmButtonProps {
  label: string
  title: string
  description: string
  disabled?: boolean
  describedBy?: string
  onConfirm: () => void
}

export function ConfirmButton({
  label,
  title,
  description,
  disabled,
  describedBy,
  onConfirm,
}: ConfirmButtonProps) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button type="button" variant="outline" disabled={disabled} aria-describedby={describedBy}>
          {label}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Закрыть</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>{label}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
