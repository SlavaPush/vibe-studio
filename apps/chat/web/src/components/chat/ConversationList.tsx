import type { Conversation } from '@/api/types'
import { Button } from '@/components/ui/button'

interface ConversationListProps {
  conversations: Conversation[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
}

export function ConversationList({ conversations, activeId, onSelect, onCreate }: ConversationListProps) {
  return (
    <nav aria-label="Диалоги" className="flex flex-col gap-2 p-3">
      <Button variant="outline" onClick={onCreate}>
        Новый диалог
      </Button>
      <ul className="flex flex-col gap-1">
        {conversations.map((conversation) => {
          const active = conversation.id === activeId
          return (
            <li key={conversation.id}>
              <Button
                variant={active ? 'secondary' : 'ghost'}
                aria-current={active ? 'page' : undefined}
                onClick={() => onSelect(conversation.id)}
                className="w-full justify-start"
              >
                <span className="truncate">{conversation.title}</span>
              </Button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
