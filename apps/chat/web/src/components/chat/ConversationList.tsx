import { Pencil } from 'lucide-react'
import { useState, type KeyboardEvent } from 'react'
import type { Conversation } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from 'cn'

interface ConversationListProps {
  conversations: Conversation[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onRename: (id: string, title: string) => void
}

export function ConversationList({ conversations, activeId, onSelect, onCreate, onRename }: ConversationListProps) {
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null)

  const finish = (save: boolean) => {
    if (save && editing) {
      const title = editing.title.trim()
      const current = conversations.find((item) => item.id === editing.id)
      if (title && title.length <= 200 && title !== current?.title) onRename(editing.id, title)
    }
    setEditing(null)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      finish(true)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      finish(false)
    }
  }

  return (
    <nav aria-label="Диалоги" className="flex flex-col gap-2 p-3">
      <Button variant="outline" onClick={onCreate} className="font-display font-bold tracking-[-0.01em] uppercase">
        Новый диалог
      </Button>
      <ul className="flex flex-col gap-1">
        {conversations.map((conversation) => {
          const active = conversation.id === activeId
          if (editing?.id === conversation.id) {
            return (
              <li key={conversation.id}>
                <Input
                  autoFocus
                  aria-label="Название диалога"
                  value={editing.title}
                  maxLength={200}
                  onChange={(event) => setEditing({ id: conversation.id, title: event.target.value })}
                  onKeyDown={onKeyDown}
                  onBlur={() => finish(true)}
                />
              </li>
            )
          }
          return (
            <li key={conversation.id} className="flex items-center gap-1">
              <Button
                variant="ghost"
                aria-current={active ? 'page' : undefined}
                onClick={() => onSelect(conversation.id)}
                className={cn(
                  'min-w-0 flex-1 justify-start rounded-full',
                  active && 'bg-brand text-brand-foreground hover:bg-brand hover:text-brand-foreground',
                )}
              >
                <span className="truncate">{conversation.title}</span>
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="rounded-full text-muted-foreground"
                aria-label={`Переименовать диалог «${conversation.title}»`}
                onClick={() => setEditing({ id: conversation.id, title: conversation.title })}
              >
                <Pencil aria-hidden="true" />
              </Button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
