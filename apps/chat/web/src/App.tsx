import { useMemo } from 'react'
import { ChatThread } from '@/components/chat/ChatThread'
import { ConversationList } from '@/components/chat/ConversationList'
import { LoginScreen } from '@/components/chat/LoginScreen'
import { ModelSelector } from '@/components/chat/ModelSelector'
import { LoadingStatus, RunErrorNotice, SessionErrorNotice, UpdateNotice } from '@/components/chat/Notices'
import type { ConversationActions } from '@/store/assistantAdapter'
import type { ChatController } from '@/store/chatController'
import { NO_CONVERSATION, useChatState, type ChatStore } from '@/store/conversations'

interface AppProps {
  store: ChatStore
  controller: ChatController
}

export default function App({ store, controller }: AppProps) {
  const state = useChatState(store, (current) => current)
  const active = state.conversations.find((item) => item.id === state.activeConversationId)
  const error = state.errors[state.activeConversationId ?? NO_CONVERSATION]
  const actions = useMemo<ConversationActions>(
    () => ({ send: (text) => controller.send(text), cancel: () => void controller.cancel() }),
    [controller],
  )

  return (
    <div className="flex h-dvh flex-col">
      {state.updateAvailable && <UpdateNotice />}
      {!state.sessionChecked ? (
        <main aria-busy="true" className="flex flex-1">
          <LoadingStatus label="Загрузка…" />
        </main>
      ) : state.sessionError ? (
        <SessionErrorNotice error={state.sessionError} onRetry={() => void controller.start()} />
      ) : state.session ? (
        <div className="flex min-h-0 flex-1">
          <aside className="w-64 shrink-0 overflow-y-auto border-r">
            <ConversationList
              conversations={state.conversations}
              activeId={state.activeConversationId}
              onSelect={(id) => void controller.selectConversation(id)}
              onCreate={() => void controller.createConversation()}
              onRename={(id, title) => void controller.renameConversation(id, title)}
            />
          </aside>
          <main className="flex min-w-0 flex-1 flex-col">
            <header className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
              <h1 className="truncate text-base font-semibold">{active?.title ?? 'vibe-chat'}</h1>
              <ModelSelector
                models={state.models}
                value={state.selectedModel}
                onChange={store.selectModel}
              />
            </header>
            <ChatThread store={store} actions={actions} loadAssetUrl={controller.loadAssetUrl} />
            {error && <RunErrorNotice error={error} />}
          </main>
        </div>
      ) : (
        <LoginScreen onSubmit={controller.signIn} />
      )}
    </div>
  )
}
