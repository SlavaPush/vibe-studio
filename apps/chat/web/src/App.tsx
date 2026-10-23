import type { AssetRef } from '@/api/types'
import { ChatThread } from '@/components/chat/ChatThread'
import { ConversationList } from '@/components/chat/ConversationList'
import { LoginScreen } from '@/components/chat/LoginScreen'
import { ModelSelector } from '@/components/chat/ModelSelector'
import { RunErrorNotice, UpdateNotice } from '@/components/chat/Notices'
import { ScenarioPanel } from '@/components/chat/ScenarioPanel'
import type { ConversationActions } from '@/store/assistantAdapter'
import { useChatState, type ChatStore } from '@/store/conversations'
import type { Scenario } from '@/mocks/scenarios'

interface AppProps {
  store: ChatStore
  scenario: Scenario
  actions: ConversationActions
  onSignIn: (code: string) => void
  resolveAssetUrl: (asset: AssetRef) => string
}

export default function App({ store, scenario, actions, onSignIn, resolveAssetUrl }: AppProps) {
  const state = useChatState(store, (current) => current)
  const active = state.conversations.find((item) => item.id === state.activeConversationId)
  const error = state.activeConversationId ? state.errors[state.activeConversationId] : undefined

  return (
    <div className="flex h-dvh flex-col">
      {state.updateAvailable && <UpdateNotice />}
      {state.session ? (
        <div className="flex min-h-0 flex-1">
          <aside className="w-64 shrink-0 overflow-y-auto border-r">
            <ConversationList
              conversations={state.conversations}
              activeId={state.activeConversationId}
              onSelect={store.selectConversation}
              onCreate={() =>
                store.addConversation({
                  id: crypto.randomUUID(),
                  title: 'Новый диалог',
                  created_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                })
              }
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
            <ChatThread store={store} actions={actions} resolveAssetUrl={resolveAssetUrl} />
            {error && <RunErrorNotice error={error} />}
          </main>
        </div>
      ) : (
        <LoginScreen onSubmit={onSignIn} />
      )}
      <ScenarioPanel current={scenario} />
    </div>
  )
}
