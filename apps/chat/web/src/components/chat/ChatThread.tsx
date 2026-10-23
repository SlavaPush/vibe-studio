import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  ThreadPrimitive,
  type MessageState,
} from '@assistant-ui/react'
import type { AssetRef, Effort, ModelProfile, SystemMessageKind } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  readMessageCustom,
  useConversationRuntime,
  type ConversationActions,
} from '@/store/assistantAdapter'
import { useChatState, type ChatStore } from '@/store/conversations'
import { cn } from 'cn'

const EFFORT_LABELS: Record<Effort, string> = {
  low: 'низкий',
  medium: 'средний',
  high: 'высокий',
  unsupported: 'не поддерживается',
}

const SYSTEM_TITLES: Record<SystemMessageKind, string> = {
  model_switched: 'Модель переключена',
  refusal: 'Запрос отклонён',
  check_unavailable: 'Проверка недоступна',
}

interface ChatThreadProps {
  store: ChatStore
  actions: ConversationActions
  resolveAssetUrl: (asset: AssetRef) => string
}

export function ChatThread({ store, actions, resolveAssetUrl }: ChatThreadProps) {
  const runtime = useConversationRuntime(store, actions)
  const models = useChatState(store, (state) => state.models)

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
        <ThreadPrimitive.Viewport
          role="log"
          aria-label="Сообщения диалога"
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 py-6"
        >
          <AuiIf condition={(state) => state.thread.isEmpty}>
            <p className="m-auto text-sm text-muted-foreground">
              Сообщений пока нет. Напишите первый запрос.
            </p>
          </AuiIf>
          <ThreadPrimitive.Messages>
            {({ message }) => (
              <ChatMessage message={message} models={models} resolveAssetUrl={resolveAssetUrl} />
            )}
          </ThreadPrimitive.Messages>
        </ThreadPrimitive.Viewport>
        <ComposerPrimitive.Root className="flex items-end gap-2 border-t p-4">
          <ComposerPrimitive.Input
            aria-label="Сообщение"
            placeholder="Напишите сообщение"
            rows={1}
            className="min-h-9 flex-1 resize-none rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
          />
          <AuiIf condition={(state) => !state.thread.isRunning}>
            <ComposerPrimitive.Send asChild>
              <Button>Отправить</Button>
            </ComposerPrimitive.Send>
          </AuiIf>
          <AuiIf condition={(state) => state.thread.isRunning}>
            <ComposerPrimitive.Cancel asChild>
              <Button variant="outline">Остановить</Button>
            </ComposerPrimitive.Cancel>
          </AuiIf>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  )
}

interface ChatMessageProps {
  message: MessageState
  models: ModelProfile[]
  resolveAssetUrl: (asset: AssetRef) => string
}

function ChatMessage({ message, models, resolveAssetUrl }: ChatMessageProps) {
  const custom = readMessageCustom(message.metadata.custom)
  const text = message.content.map((part) => (part.type === 'text' ? part.text : '')).join('')

  if (message.role === 'system') {
    const kind = custom.kind ?? 'model_switched'
    return (
      <div
        role="note"
        aria-label={SYSTEM_TITLES[kind]}
        data-kind={kind}
        className={cn(
          'mx-auto w-full max-w-prose rounded-md border px-3 py-2 text-sm',
          kind === 'refusal'
            ? 'border-destructive/50 text-destructive'
            : 'border-dashed bg-muted text-muted-foreground',
        )}
      >
        <p className="font-medium">{SYSTEM_TITLES[kind]}</p>
        <p>{text}</p>
      </div>
    )
  }

  if (message.role === 'user') {
    return (
      <div className="ml-auto max-w-[80%] rounded-lg bg-secondary px-3 py-2 text-sm whitespace-pre-wrap text-secondary-foreground">
        {text}
      </div>
    )
  }

  const model = models.find((profile) => profile.id === custom.effective_model)
  const isPending = message.status?.type === 'running'
  return (
    <article aria-label="Ответ ассистента" className="flex max-w-[80%] flex-col gap-2 text-sm">
      {custom.asset && (
        <Card className="w-64 overflow-hidden py-0">
          <CardContent className="p-0">
            <img
              src={resolveAssetUrl(custom.asset)}
              alt="Сгенерированное изображение"
              width={custom.asset.width ?? undefined}
              height={custom.asset.height ?? undefined}
              className="h-auto w-full"
            />
          </CardContent>
        </Card>
      )}
      {text && <p className="whitespace-pre-wrap">{text}</p>}
      {isPending && (
        <p role="status" className="text-muted-foreground">
          Ответ готовится…
        </p>
      )}
      {custom.effective_model && (
        <p className="text-xs text-muted-foreground">
          Модель: {model?.label ?? custom.effective_model}
          {custom.effort && ` · усилия: ${EFFORT_LABELS[custom.effort]}`}
        </p>
      )}
    </article>
  )
}
