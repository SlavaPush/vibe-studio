import { LogOut, Menu, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { AboutDialog } from '@/components/chat/AboutDialog'
import { ChatThread } from '@/components/chat/ChatThread'
import { ConversationList } from '@/components/chat/ConversationList'
import { LoginScreen } from '@/components/chat/LoginScreen'
import { MessageQueue } from '@/components/chat/MessageQueue'
import { ModelSelector } from '@/components/chat/ModelSelector'
import {
  LoadingStatus,
  RunErrorNotice,
  SessionErrorNotice,
  UpdateDialog,
  UpdateNotice,
} from '@/components/chat/Notices'
import { ThemeToggle } from '@/components/ThemeToggle'
import { Button } from '@/components/ui/button'
import { Sheet, SheetClose, SheetContent, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import type { ConversationActions } from '@/store/assistantAdapter'
import type { ChatController } from '@/store/chatController'
import { hasActiveRun, NO_CONVERSATION, useChatState, type ChatStore } from '@/store/conversations'
import { buildVersion } from '@/store/versionWatch'

interface AppProps {
  store: ChatStore
  controller: ChatController
  version?: string
}

const DESKTOP_QUERY = '(min-width: 768px)'

const COMPOSER_INPUT_ID = 'composer-input'

export default function App({ store, controller, version = buildVersion() }: AppProps) {
  const state = useChatState(store, (current) => current)
  const active = state.conversations.find((item) => item.id === state.activeConversationId)
  const error = state.errors[state.activeConversationId ?? NO_CONVERSATION]
  const listFailed = state.errors[NO_CONVERSATION] !== undefined
  const versionLabel = (
    <p className="mt-auto px-3 pt-2 pb-3 text-xs text-muted-foreground [overflow-wrap:anywhere]">Версия: {version}</p>
  )
  const actions = useMemo<ConversationActions>(
    () => ({ send: (text) => controller.send(text), cancel: () => void controller.cancel() }),
    [controller],
  )
  const [updatePrompt, setUpdatePrompt] = useState<'pending' | 'open' | 'closed'>('pending')
  const [navOpen, setNavOpen] = useState(false)
  if (
    state.updateAvailable &&
    updatePrompt === 'pending' &&
    !state.sending &&
    !hasActiveRun(state) &&
    state.queue.length === 0
  ) {
    setUpdatePrompt('open')
  }

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(DESKTOP_QUERY)
    const close = () => {
      if (query.matches) setNavOpen(false)
    }
    query.addEventListener('change', close)
    return () => query.removeEventListener('change', close)
  }, [])

  return (
    <div className="flex h-dvh flex-col pr-[env(safe-area-inset-right)] pl-[env(safe-area-inset-left)]">
      {state.updateAvailable && updatePrompt !== 'open' && <UpdateNotice />}
      <UpdateDialog open={updatePrompt === 'open'} onLater={() => setUpdatePrompt('closed')} />
      {!state.sessionChecked ? (
        <main aria-busy="true" className="flex flex-1">
          <LoadingStatus label="Загрузка…" />
        </main>
      ) : state.sessionError ? (
        <SessionErrorNotice error={state.sessionError} onRetry={() => void controller.start()} />
      ) : state.session ? (
        <Sheet open={navOpen} onOpenChange={setNavOpen}>
          <a
            href={`#${COMPOSER_INPUT_ID}`}
            onClick={(event) => {
              event.preventDefault()
              document.getElementById(COMPOSER_INPUT_ID)?.focus()
            }}
            className="sr-only focus:not-sr-only focus:m-2 focus:self-start focus:rounded-md focus:bg-background focus:px-3 focus:py-2 focus:text-sm focus:underline"
          >
            Перейти к полю сообщения
          </a>
          <div className="flex min-h-0 flex-1">
            <aside className="hidden w-64 shrink-0 flex-col overflow-y-auto border-r md:flex">
              <ConversationList
                conversations={state.conversations}
                activeId={state.activeConversationId}
                onSelect={(id) => void controller.selectConversation(id)}
                onCreate={() => void controller.createConversation()}
                onRename={(id, title) => void controller.renameConversation(id, title)}
                loadFailed={listFailed}
              />
              {versionLabel}
            </aside>
            <SheetContent aria-describedby={undefined}>
              <div className="flex items-center justify-between gap-2 border-b py-1 pr-1 pl-3">
                <SheetTitle>Диалоги</SheetTitle>
                <SheetClose asChild>
                  <Button variant="ghost" size="icon" aria-label="Закрыть список диалогов">
                    <X aria-hidden="true" />
                  </Button>
                </SheetClose>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                <ConversationList
                  conversations={state.conversations}
                  activeId={state.activeConversationId}
                  onSelect={(id) => {
                    setNavOpen(false)
                    void controller.selectConversation(id)
                  }}
                  onCreate={() => {
                    setNavOpen(false)
                    void controller.createConversation()
                  }}
                  onRename={(id, title) => void controller.renameConversation(id, title)}
                  loadFailed={listFailed}
                />
              </div>
              {versionLabel}
            </SheetContent>
            <main className="flex min-w-0 flex-1 flex-col">
              <header className="flex flex-col gap-2 border-b px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-2 md:flex-row md:flex-wrap md:items-center md:justify-between md:gap-3 md:px-4 md:py-3">
                <div className="flex min-w-0 items-center gap-2">
                  <SheetTrigger asChild>
                    <Button variant="ghost" size="icon" className="md:hidden" aria-label="Открыть список диалогов">
                      <Menu aria-hidden="true" />
                    </Button>
                  </SheetTrigger>
                  <h1 className="min-w-0 flex-1 truncate text-base font-semibold">{active?.title ?? 'vibe-chat'}</h1>
                </div>
                <div className="flex min-w-0 flex-wrap items-center gap-2 md:gap-3">
                  <ModelSelector
                    models={state.models}
                    value={state.selectedModel}
                    onChange={store.selectModel}
                  />
                  <AboutDialog />
                  <ThemeToggle className="shrink-0" />
                  <Button variant="outline" size="sm" className="shrink-0" onClick={() => store.signOut()}>
                    <LogOut aria-hidden="true" />
                    Выйти
                  </Button>
                </div>
              </header>
              <ChatThread
                store={store}
                actions={actions}
                loadAssetUrl={controller.loadAssetUrl}
                queue={
                  <MessageQueue store={store} onRemove={controller.removeQueued} onResume={controller.resumeQueue} />
                }
              />
              {error && <RunErrorNotice error={error} />}
            </main>
          </div>
        </Sheet>
      ) : (
        <LoginScreen onSubmit={controller.signIn} />
      )}
    </div>
  )
}
