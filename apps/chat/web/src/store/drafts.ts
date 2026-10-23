export function draftKey(userId: string, conversationId: string | null): string {
  return `vibe-chat:draft:${userId}:${conversationId ?? 'new'}`
}

export function activeConversationKey(userId: string): string {
  return `vibe-chat:active:${userId}`
}

export function readLocal(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

export function writeLocal(key: string, text: string): void {
  try {
    if (text) window.localStorage.setItem(key, text)
    else window.localStorage.removeItem(key)
  } catch {
    return
  }
}
