import type {
  CheckReport,
  Participant,
  ReleaseCandidate,
  Task,
  TaskEvent,
} from '@/api/types'
import examples from './contract-examples.json'

export const participants: Participant[] = [
  ...(examples.Participant as Participant[]),
  {
    id: 'prt_ivan',
    display_name: 'Иван',
    role: 'participant',
    expires_at: '2026-10-24T09:00:00Z',
  },
]

export const tasks = examples['Task[]'][0] as Task[]

export const events = [...examples['TaskEvent[]'].flat(), ...examples.TaskEvent] as TaskEvent[]

export const releases = examples['ReleaseCandidate[]'][0] as ReleaseCandidate[]

export const checkReports: CheckReport[] = [
  examples.CheckReport[0] as CheckReport,
  {
    task_id: 'tsk_102',
    commit_sha: 'c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6',
    spec_hash: 'sha256:9c1e4b7a2d3f5e60',
    suite_hash: 'sha256:71c0d2e93a4b5f18',
    checks: [
      {
        command: 'npm run lint',
        exit_code: 0,
        duration_ms: 2150,
        artifact_urls: ['/api/tasks/tsk_102/artifacts/lint.log'],
      },
      {
        command: 'npm run build',
        exit_code: 0,
        duration_ms: 17980,
        artifact_urls: ['/api/tasks/tsk_102/artifacts/build.log'],
      },
      {
        command: 'npx playwright test e2e/chat.spec.ts',
        exit_code: 0,
        duration_ms: 39500,
        artifact_urls: ['/api/tasks/tsk_102/artifacts/e2e.log'],
      },
    ],
  },
]

export const diffs: Record<string, string> = {
  tsk_101: `diff --git a/web/src/components/chat/MessageActions.tsx b/web/src/components/chat/MessageActions.tsx
new file mode 100644
--- /dev/null
+++ b/web/src/components/chat/MessageActions.tsx
@@ -0,0 +1,12 @@
+import { useState } from 'react'
+
+export function MessageActions({ text }: { text: string }) {
+  const [copied, setCopied] = useState(false)
+  const copy = async () => {
+    await navigator.clipboard.writeText(text)
+    setCopied(true)
+    setTimeout(() => setCopied(false), 2000)
+  }
+  return <button type="button" onClick={copy}>{copied ? 'Скопировано' : 'Копировать'}</button>
+}
diff --git a/web/src/components/chat/AssistantMessage.tsx b/web/src/components/chat/AssistantMessage.tsx
--- a/web/src/components/chat/AssistantMessage.tsx
+++ b/web/src/components/chat/AssistantMessage.tsx
@@ -8,6 +8,7 @@ export function AssistantMessage({ message }: Props) {
   return (
     <article>
       <Markdown text={message.text} />
+      {message.done && <MessageActions text={message.text} />}
     </article>
   )
 }
`,
  tsk_102: `diff --git a/web/src/components/settings/ThemeSwitch.tsx b/web/src/components/settings/ThemeSwitch.tsx
new file mode 100644
--- /dev/null
+++ b/web/src/components/settings/ThemeSwitch.tsx
@@ -0,0 +1,9 @@
+export function ThemeSwitch({ dark, onChange }: { dark: boolean; onChange: (dark: boolean) => void }) {
+  return (
+    <label>
+      <input type="checkbox" role="switch" checked={dark} onChange={(event) => onChange(event.target.checked)} />
+      Тёмная тема
+    </label>
+  )
+}
+
diff --git a/web/src/components/settings/SettingsDialog.tsx b/web/src/components/settings/SettingsDialog.tsx
--- a/web/src/components/settings/SettingsDialog.tsx
+++ b/web/src/components/settings/SettingsDialog.tsx
@@ -14,6 +14,7 @@ export function SettingsDialog({ settings, onChange }: Props) {
     <section>
       <h2>Настройки</h2>
+      <ThemeSwitch dark={settings.dark} onChange={(dark) => onChange({ ...settings, dark })} />
       <ModelSelect value={settings.model} onChange={(model) => onChange({ ...settings, model })} />
     </section>
   )
`,
}
