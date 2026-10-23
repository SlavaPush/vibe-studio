export const VERSION_CHECK_INTERVAL = 5 * 60 * 1000

export function buildVersion(doc: Document = document): string {
  return doc.querySelector<HTMLMetaElement>('meta[name="app-version"]')?.content || 'dev'
}

export function watchVersion(
  current: string,
  onUpdate: () => void,
  fetchImpl: typeof fetch = (...args) => fetch(...args),
): () => void {
  let notified = false

  async function check() {
    if (notified) return
    try {
      const response = await fetchImpl('/api/version', { cache: 'no-store' })
      if (!response.ok) return
      const data = (await response.json()) as { version?: unknown }
      if (notified || typeof data.version !== 'string' || data.version === current) return
      notified = true
      onUpdate()
    } catch {
      return
    }
  }

  const onVisibility = () => {
    if (document.visibilityState === 'visible') void check()
  }
  document.addEventListener('visibilitychange', onVisibility)
  const timer = window.setInterval(() => void check(), VERSION_CHECK_INTERVAL)
  return () => {
    document.removeEventListener('visibilitychange', onVisibility)
    window.clearInterval(timer)
  }
}
