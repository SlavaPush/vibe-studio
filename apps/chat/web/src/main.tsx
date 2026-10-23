import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { createApiClient } from './api/client.ts'
import { createChatController } from './store/chatController.ts'
import { createChatStore } from './store/conversations.ts'
import { buildVersion, watchVersion } from './store/versionWatch.ts'

const darkScheme = window.matchMedia('(prefers-color-scheme: dark)')
const applyTheme = () => document.documentElement.classList.toggle('dark', darkScheme.matches)
applyTheme()
darkScheme.addEventListener('change', applyTheme)

const store = createChatStore()
const controller = createChatController(store, createApiClient())
void controller.start()
watchVersion(buildVersion(), store.showUpdate)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App store={store} controller={controller} />
  </StrictMode>,
)
