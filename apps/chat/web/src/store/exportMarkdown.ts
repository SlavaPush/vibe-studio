import type { Message } from '@/api/types'

const ROLE_LABELS: Partial<Record<Message['role'], string>> = {
  user: 'Пользователь',
  assistant: 'Ассистент',
}

const FALLBACK_TITLE = 'Диалог'
const MAX_FILE_NAME_LENGTH = 80
const REVOKE_DELAY_MS = 1000

export function exportableMessages(messages: Message[]): Message[] {
  return messages.filter((message) => message.role === 'user' || message.role === 'assistant')
}

export function buildMarkdown(title: string, messages: Message[]): string {
  const heading = title.replace(/\s+/g, ' ').trim() || FALLBACK_TITLE
  const parts = [`# ${heading}`]
  for (const message of exportableMessages(messages)) {
    parts.push(`## ${ROLE_LABELS[message.role] ?? ''}\n\n${message.text}`)
  }
  return `${parts.join('\n\n')}\n`
}

export function markdownFileName(title: string): string {
  const safe = title
    .replace(/[^\p{L}\p{N}_ -]+/gu, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, MAX_FILE_NAME_LENGTH)
    .trim()
  return `${safe || 'dialog'}.md`
}

export function downloadMarkdown(fileName: string, content: string): void {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS)
}
