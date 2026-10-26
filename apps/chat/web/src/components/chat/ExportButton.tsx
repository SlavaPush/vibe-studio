import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useChatState, type ChatStore } from '@/store/conversations'
import { buildMarkdown, downloadMarkdown, exportableMessages, markdownFileName } from '@/store/exportMarkdown'

const TOOLTIP_TEXT = 'Диалог будет сохранён в файл Markdown'
const TOOLTIP_GAP = 8
const VIEWPORT_MARGIN = 8

export function ExportButton({ store }: { store: ChatStore }) {
  const conversation = useChatState(store, (state) =>
    state.conversations.find((item) => item.id === state.activeConversationId),
  )
  const messages = useChatState(store, (state) =>
    state.activeConversationId ? state.messages[state.activeConversationId] : undefined,
  )
  const exportable = messages ? exportableMessages(messages) : []
  const disabled = !conversation || exportable.length === 0

  const tooltipId = useId()
  const wrapperRef = useRef<HTMLSpanElement>(null)
  const tooltipRef = useRef<HTMLSpanElement>(null)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const [position, setPosition] = useState<{ top: number; left: number }>({ top: 0, left: 0 })
  const visible = (hovered || focused) && !dismissed

  useEffect(() => {
    if (!visible) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDismissed(true)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [visible])

  useLayoutEffect(() => {
    if (!visible) return
    const wrapper = wrapperRef.current
    const tooltip = tooltipRef.current
    if (!wrapper || !tooltip) return
    const update = () => {
      const anchor = wrapper.getBoundingClientRect()
      const width = tooltip.offsetWidth
      const height = tooltip.offsetHeight
      let top = anchor.bottom + TOOLTIP_GAP
      if (top + height > window.innerHeight - VIEWPORT_MARGIN) {
        const above = anchor.top - TOOLTIP_GAP - height
        if (above >= VIEWPORT_MARGIN) top = above
      }
      const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - width - VIEWPORT_MARGIN)
      const left = Math.min(Math.max(anchor.right - width, VIEWPORT_MARGIN), maxLeft)
      setPosition({ top, left })
    }
    update()
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [visible])

  const handleClick = () => {
    if (!conversation) return
    downloadMarkdown(markdownFileName(conversation.title), buildMarkdown(conversation.title, exportable))
  }

  const handleMouseEnter = () => {
    setDismissed(false)
    setHovered(true)
  }
  const handleMouseLeave = () => {
    setHovered(false)
    if (!focused) setDismissed(false)
  }
  const handleFocus = () => {
    setDismissed(false)
    setFocused(true)
  }
  const handleBlur = () => {
    setFocused(false)
    if (!hovered) setDismissed(false)
  }

  return (
    <span
      ref={wrapperRef}
      className="relative inline-flex shrink-0"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onFocus={handleFocus}
      onBlur={handleBlur}
    >
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="shrink-0 md:w-auto md:px-4"
        disabled={disabled}
        aria-describedby={visible ? tooltipId : undefined}
        onClick={handleClick}
      >
        <Download aria-hidden="true" className="md:hidden" />
        <span className="sr-only md:not-sr-only">Экспорт в Markdown</span>
      </Button>
      {visible ? (
        <span
          ref={tooltipRef}
          id={tooltipId}
          role="tooltip"
          style={{ top: position.top, left: position.left }}
          className="pointer-events-none fixed z-50 w-max max-w-[calc(100vw-1rem)] rounded-md border bg-popover px-3 py-1.5 text-xs text-popover-foreground shadow-md"
        >
          {TOOLTIP_TEXT}
        </span>
      ) : null}
    </span>
  )
}
