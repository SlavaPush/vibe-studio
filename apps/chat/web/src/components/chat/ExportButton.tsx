import { Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useChatState, type ChatStore } from '@/store/conversations'
import { buildMarkdown, downloadMarkdown, exportableMessages, markdownFileName } from '@/store/exportMarkdown'

export function ExportButton({ store }: { store: ChatStore }) {
  const conversation = useChatState(store, (state) =>
    state.conversations.find((item) => item.id === state.activeConversationId),
  )
  const messages = useChatState(store, (state) =>
    state.activeConversationId ? state.messages[state.activeConversationId] : undefined,
  )
  const exportable = messages ? exportableMessages(messages) : []
  const disabled = !conversation || exportable.length === 0

  const handleClick = () => {
    if (!conversation) return
    downloadMarkdown(markdownFileName(conversation.title), buildMarkdown(conversation.title, exportable))
  }

  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      className="shrink-0 md:w-auto md:px-4"
      disabled={disabled}
      onClick={handleClick}
    >
      <Download aria-hidden="true" className="md:hidden" />
      <span className="sr-only md:not-sr-only">Экспорт в Markdown</span>
    </Button>
  )
}
