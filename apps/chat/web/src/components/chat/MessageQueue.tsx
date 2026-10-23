import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { QUEUE_LIMIT, useChatState, type ChatStore, type QueuedMessage, type QueuePause } from '@/store/conversations'

const PAUSE_REASONS: Record<QueuePause['reason'], string> = {
  failed: 'ответ не получен',
  cancelled: 'ответ остановлен',
  check_unavailable: 'проверка сообщения недоступна',
  request_failed: 'сообщение не отправлено',
}

interface MessageQueueProps {
  store: ChatStore
  onRemove: (id: string) => void
  onResume: () => void
}

function announce(previous: QueuedMessage[], queue: QueuedMessage[]): string {
  const count = `В очереди: ${queue.length} из ${QUEUE_LIMIT}.`
  const ids = new Set(queue.map((item) => item.id))
  const removed = previous.filter((item) => !ids.has(item.id)).length
  const added = queue.length - (previous.length - removed)
  if (added > 0) return `Сообщение добавлено в очередь. ${count}`
  if (removed > 0) return `Сообщение убрано из очереди. ${count}`
  return ''
}

export function MessageQueue({ store, onRemove, onResume }: MessageQueueProps) {
  const queue = useChatState(store, (state) => state.queue)
  const pause = useChatState(store, (state) => state.queuePause)
  const sendingId = useChatState(store, (state) => state.sendingQueuedId)
  const activeId = useChatState(store, (state) => state.activeConversationId)
  const conversations = useChatState(store, (state) => state.conversations)
  const [previous, setPrevious] = useState(queue)
  const [announcement, setAnnouncement] = useState('')
  if (previous !== queue) {
    setPrevious(queue)
    setAnnouncement(announce(previous, queue) || announcement)
  }

  const title = (id: string) => conversations.find((item) => item.id === id)?.title ?? 'другой диалог'

  return (
    <>
      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>
      {queue.length > 0 && (
        <section aria-label="Очередь сообщений" className="flex flex-col gap-2 border-t px-4 py-3 text-sm">
          <h2 className="text-xs font-medium text-muted-foreground">
            В очереди: {queue.length} из {QUEUE_LIMIT}
          </h2>
          {pause && (
            <div className="flex flex-wrap items-center gap-2">
              <p role="status" className="text-destructive">
                Очередь приостановлена: {pause.reason === 'request_failed' && pause.error ? pause.error.message : PAUSE_REASONS[pause.reason]}
              </p>
              <Button size="sm" variant="outline" onClick={onResume}>
                Продолжить
              </Button>
            </div>
          )}
          <ol className="flex flex-col gap-1">
            {queue.map((item, index) => (
              <li key={item.id} className="flex items-start gap-2 rounded-md border px-3 py-2">
                <span className="text-muted-foreground">{index + 1}.</span>
                <div className="flex min-w-0 flex-1 flex-col">
                  <p className="line-clamp-2 break-words whitespace-pre-wrap">{item.text}</p>
                  {item.conversationId !== activeId && (
                    <p className="text-xs text-muted-foreground">Диалог: {title(item.conversationId)}</p>
                  )}
                </div>
                {item.id === sendingId ? (
                  <span className="text-xs text-muted-foreground">Отправляется…</span>
                ) : (
                  <Button
                    size="xs"
                    variant="ghost"
                    aria-label={`Убрать из очереди сообщение ${index + 1}`}
                    onClick={() => onRemove(item.id)}
                  >
                    Убрать
                  </Button>
                )}
              </li>
            ))}
          </ol>
          {queue.length >= QUEUE_LIMIT && (
            <p className="text-xs text-muted-foreground">
              Очередь заполнена: не больше {QUEUE_LIMIT} сообщений. Дождитесь ответа или уберите сообщение из очереди.
            </p>
          )}
        </section>
      )}
    </>
  )
}
