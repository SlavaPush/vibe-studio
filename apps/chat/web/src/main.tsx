import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { cancelMockRun, sendMockMessage } from './mocks/runs.ts'
import { createScenarioState, mockAssetUrl, mockSession, parseScenario } from './mocks/scenarios.ts'
import { createChatStore } from './store/conversations.ts'

const darkScheme = window.matchMedia('(prefers-color-scheme: dark)')
const applyTheme = () => document.documentElement.classList.toggle('dark', darkScheme.matches)
applyTheme()
darkScheme.addEventListener('change', applyTheme)

const scenario = parseScenario(window.location.search)
const store = createChatStore(createScenarioState(scenario))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App
      store={store}
      scenario={scenario}
      actions={{
        send: (text) => sendMockMessage(store, text),
        cancel: () => cancelMockRun(store),
      }}
      onSignIn={() => store.signIn(mockSession)}
      resolveAssetUrl={mockAssetUrl}
    />
  </StrictMode>,
)
