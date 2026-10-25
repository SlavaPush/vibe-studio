import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
  type AssistantRuntime,
  type MessageState,
} from '@assistant-ui/react'
import { ArrowUp, ListPlus, Square } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiRequestError } from '@/api/client'
import type { AssetRef, AssetUrl, Effort, ModelProfile, SystemMessageKind } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { LoadingStatus } from '@/components/chat/Notices'
import {
  readMessageCustom,
  useConversationRuntime,
  type ConversationActions,
} from '@/store/assistantAdapter'
import { activeRunId, isSendBlocked, useChatState, type ChatStore } from '@/store/conversations'
import { draftKey, readLocal, writeLocal } from '@/store/drafts'
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

const SYSTEM_HINTS: Partial<Record<SystemMessageKind, string>> = {
  model_switched: 'Выбор модели в списке не изменился.',
  check_unavailable: 'Сообщение не передано модели. Повторите позже.',
}

const COMPOSER_BUTTON = 'rounded-full md:h-9 md:w-auto md:rounded-md md:px-4'

const COMPOSER_HINT_ID = 'composer-hint'

const MAX_MESSAGE_LENGTH = 4000

const THINKING_DOT_DELAYS = ['[animation-delay:0ms]', '[animation-delay:200ms]', '[animation-delay:400ms]']

export type LoadAssetUrl = (id: string) => Promise<AssetUrl>

interface ChatThreadProps {
  store: ChatStore
  actions: ConversationActions
  loadAssetUrl: LoadAssetUrl
  queue?: ReactNode
}

export function ChatThread({ store, actions, loadAssetUrl, queue }: ChatThreadProps) {
  const storageKey = useChatState(store, (state) =>
    draftKey(state.session?.user_id ?? '', state.activeConversationId),
  )
  const runtimeRef = useRef<AssistantRuntime | null>(null)
  const threadActions = useMemo<ConversationActions>(
    () => ({
      ...actions,
      send: async (text) => {
        const userId = store.getState().session?.user_id ?? ''
        let target = store.getState().activeConversationId
        writeLocal(draftKey(userId, target), '')
        let sent = false
        try {
          const result = (await actions.send(text)) ?? { sent: true, conversationId: target }
          sent = result.sent
          target = result.conversationId
          return result
        } finally {
          if (!sent) {
            const composer = runtimeRef.current?.thread.composer
            if (composer && store.getState().activeConversationId === target) {
              composer.setText(joinDraft(text, composer.getState().text))
            } else {
              const key = draftKey(userId, target)
              writeLocal(key, joinDraft(text, readLocal(key)))
            }
          }
        }
      },
    }),
    [actions, store],
  )
  const runtime = useConversationRuntime(store, threadActions)
  useEffect(() => {
    runtimeRef.current = runtime
  }, [runtime])
  const models = useChatState(store, (state) => state.models)
  const sendBlocked = useChatState(store, isSendBlocked)
  const running = useChatState(store, (state) => activeRunId(state) !== null)
  const enqueueComposer = () => {
    const composer = runtimeRef.current?.thread.composer
    const text = composer?.getState().text.trim()
    if (!composer || !text || isSendBlocked(store.getState())) return
    composer.setText('')
    void threadActions.send(text)
  }
  const historyStatus = useChatState(store, (state) => {
    const id = state.activeConversationId
    if (!id || state.loaded[id]) return 'ready'
    return state.errors[id] ? 'failed' : 'loading'
  })
  const noConversation = useChatState(store, (state) => state.activeConversationId === null)

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ComposerDraft storageKey={storageKey} />
      <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
        <ThreadPrimitive.Viewport
          role="log"
          aria-label="Сообщения диалога"
          tabIndex={0}
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-3 py-4 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset md:px-4 md:py-6"
        >
          {historyStatus === 'loading' && <LoadingStatus label="Загрузка сообщений…" />}
          {historyStatus === 'ready' && (
            <AuiIf condition={(state) => state.thread.isEmpty}>
              <p className="m-auto text-center text-sm text-muted-foreground">
                {noConversation
                  ? 'Напишите сообщение, чтобы начать диалог.'
                  : 'Сообщений пока нет. Напишите первый запрос.'}
              </p>
            </AuiIf>
          )}
          <ThreadPrimitive.Messages>
            {({ message }) => (
              <ChatMessage message={message} models={models} loadAssetUrl={loadAssetUrl} />
            )}
          </ThreadPrimitive.Messages>
        </ThreadPrimitive.Viewport>
        {queue}
        <div className="border-t p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <ComposerPrimitive.Root className="flex items-end gap-2">
            <ComposerPrimitive.Input
              id="composer-input"
              aria-label="Сообщение"
              aria-describedby={noConversation ? undefined : COMPOSER_HINT_ID}
              placeholder="Напишите сообщение"
              rows={1}
              onKeyDown={(event) => {
                if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
                if (sendBlocked || running) event.preventDefault()
                if (!sendBlocked && running) enqueueComposer()
              }}
              className="max-h-40 min-h-10 min-w-0 flex-1 resize-none overflow-y-auto rounded-md border border-input bg-transparent px-3 py-2 text-base shadow-xs md:min-h-9 md:text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring"
            />
            <AuiIf condition={(state) => !state.thread.isRunning}>
              <ComposerPrimitive.Send asChild>
                <Button size="icon" className={COMPOSER_BUTTON}>
                  <ArrowUp className="md:hidden" />
                  <span className="sr-only md:not-sr-only">Отправить</span>
                </Button>
              </ComposerPrimitive.Send>
            </AuiIf>
            <AuiIf condition={(state) => state.thread.isRunning}>
              <EnqueueButton disabled={sendBlocked} onClick={enqueueComposer} />
              <ComposerPrimitive.Cancel asChild>
                <Button variant="outline" size="icon" className={COMPOSER_BUTTON}>
                  <Square className="fill-current md:hidden" />
                  <span className="sr-only md:not-sr-only">Остановить</span>
                </Button>
              </ComposerPrimitive.Cancel>
            </AuiIf>
          </ComposerPrimitive.Root>
          <ComposerCounter />
          {!noConversation && (
            <p id={COMPOSER_HINT_ID} className="mt-2 text-xs text-muted-foreground">
              Enter — отправить, Shift+Enter — новая строка
            </p>
          )}
        </div>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  )
}

function ComposerCounter() {
  const length = useAuiState((state) => state.composer.text.length)
  return (
    <p
      data-testid="composer-counter"
      aria-live="polite"
      className="mt-1 text-right text-xs text-muted-foreground"
    >
      {`${length} / ${MAX_MESSAGE_LENGTH}`}
    </p>
  )
}

function EnqueueButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  const empty = useAuiState((state) => state.composer.isEmpty)
  return (
    <Button
      type="button"
      size="icon"
      className={COMPOSER_BUTTON}
      disabled={disabled || empty}
      onClick={onClick}
    >
      <ListPlus className="md:hidden" />
      <span className="sr-only md:not-sr-only">В очередь</span>
    </Button>
  )
}

function joinDraft(text: string, current: string): string {
  return current ? `${text}\n${current}` : text
}

function ComposerDraft({ storageKey }: { storageKey: string }) {
  const aui = useAui()
  const text = useAuiState((state) => state.composer.text)
  const restoredKey = useRef<string | null>(null)

  useEffect(() => {
    if (restoredKey.current !== storageKey) {
      restoredKey.current = storageKey
      aui.composer.setText(readLocal(storageKey))
      return
    }
    writeLocal(storageKey, text)
  }, [aui, storageKey, text])

  return null
}

interface ChatMessageProps {
  message: MessageState
  models: ModelProfile[]
  loadAssetUrl: LoadAssetUrl
}

function AssetImage({ asset, loadAssetUrl }: { asset: AssetRef; loadAssetUrl: LoadAssetUrl }) {
  const [url, setUrl] = useState<AssetUrl | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [reloaded, setReloaded] = useState(false)

  useEffect(() => {
    let active = true
    loadAssetUrl(asset.id)
      .then((next) => {
        if (!active) return
        setUrl(next)
        setError(null)
      })
      .catch((reason: unknown) => {
        if (!active) return
        setError(reason instanceof ApiRequestError ? reason.error.message : 'Не удалось загрузить изображение.')
      })
    return () => {
      active = false
    }
  }, [asset.id, attempt, loadAssetUrl])

  useEffect(() => {
    if (!url) return
    const delay = Math.max(new Date(url.expires_at).getTime() - Date.now() - 5000, 1000)
    const timer = window.setTimeout(() => setAttempt((value) => value + 1), delay)
    return () => window.clearTimeout(timer)
  }, [url])

  return (
    <Card className="w-64 max-w-full overflow-hidden py-0">
      <CardContent className="p-0">
        {error ? (
          <div role="alert" className="flex flex-col gap-2 p-3 text-sm [overflow-wrap:anywhere] text-destructive">
            <p>{error}</p>
            <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>
              Повторить
            </Button>
          </div>
        ) : url ? (
          <img
            src={url.url}
            alt="Сгенерированное изображение"
            width={asset.width ?? undefined}
            height={asset.height ?? undefined}
            className="h-auto w-full"
            onLoad={() => setReloaded(false)}
            onError={() => {
              if (reloaded) {
                setError('Не удалось загрузить изображение.')
                return
              }
              setReloaded(true)
              setAttempt((value) => value + 1)
            }}
          />
        ) : (
          <p role="status" className="p-3 text-sm text-muted-foreground">
            Изображение загружается…
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function ChatMessage({ message, models, loadAssetUrl }: ChatMessageProps) {
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
          'mx-auto w-full max-w-prose rounded-md border px-3 py-2 text-sm [overflow-wrap:anywhere]',
          kind === 'refusal'
            ? 'border-destructive/50 text-destructive'
            : 'border-dashed bg-muted text-muted-foreground',
        )}
      >
        <p className="font-medium">{SYSTEM_TITLES[kind]}</p>
        <p>{text}</p>
        {SYSTEM_HINTS[kind] && <p>{SYSTEM_HINTS[kind]}</p>}
      </div>
    )
  }

  if (message.role === 'user') {
    return (
      <div className="ml-auto max-w-[85%] rounded-lg bg-secondary px-3 py-2 text-sm [overflow-wrap:anywhere] whitespace-pre-wrap text-secondary-foreground md:max-w-[80%]">
        {text}
      </div>
    )
  }

  const model = models.find((profile) => profile.id === custom.effective_model)
  const isPending = message.status?.type === 'running'
  return (
    <article
      aria-label="Ответ ассистента"
      className="flex max-w-[85%] min-w-0 flex-col gap-2 text-sm [overflow-wrap:anywhere] md:max-w-[80%]"
    >
      {custom.asset && <AssetImage asset={custom.asset} loadAssetUrl={loadAssetUrl} />}
      {text && <p className="whitespace-pre-wrap">{text}</p>}
      {custom.incomplete && <p className="text-xs text-destructive">Ответ не завершён</p>}
      {isPending && !custom.incomplete && (
        <p role="status" className="flex items-center gap-0.5 text-muted-foreground">
          Думаю<span className="sr-only">…</span>
          <span aria-hidden="true" className="inline-flex translate-y-0.5 gap-1 pl-0.5">
            {THINKING_DOT_DELAYS.map((delay) => (
              <span
                key={delay}
                className={cn('size-1 animate-thinking-dot rounded-full bg-current motion-reduce:animate-none', delay)}
              />
            ))}
          </span>
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
