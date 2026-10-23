import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { buildVersion, VERSION_CHECK_INTERVAL, watchVersion } from './versionWatch'

function versionResponse(version: string): Response {
  return new Response(JSON.stringify({ version }), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

function showTab() {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('watchVersion', () => {
  let stop: () => void = () => {}

  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    stop()
    vi.useRealTimers()
  })

  it('при другой версии сервера при возвращении на вкладку сообщает о новой версии один раз', async () => {
    const fetchImpl = vi.fn().mockImplementation(async () => versionResponse('v2'))
    const onUpdate = vi.fn()
    stop = watchVersion('v1', onUpdate, fetchImpl)

    showTab()
    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1))
    expect(fetchImpl).toHaveBeenCalledWith('/api/version', { cache: 'no-store' })

    showTab()
    await vi.advanceTimersByTimeAsync(VERSION_CHECK_INTERVAL)
    expect(onUpdate).toHaveBeenCalledTimes(1)
  })

  it('проверяет версию по интервалу', async () => {
    const fetchImpl = vi.fn().mockImplementation(async () => versionResponse('v2'))
    const onUpdate = vi.fn()
    stop = watchVersion('v1', onUpdate, fetchImpl)

    await vi.advanceTimersByTimeAsync(VERSION_CHECK_INTERVAL - 1)
    expect(fetchImpl).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await vi.waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1))
  })

  it('при той же версии, ошибке сети и ответе с ошибкой уведомления нет', async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementationOnce(async () => versionResponse('v1'))
      .mockImplementationOnce(async () => {
        throw new TypeError('Failed to fetch')
      })
      .mockImplementationOnce(async () => new Response('{}', { status: 503 }))
    const onUpdate = vi.fn()
    stop = watchVersion('v1', onUpdate, fetchImpl)

    for (let i = 0; i < 3; i += 1) await vi.advanceTimersByTimeAsync(VERSION_CHECK_INTERVAL)
    expect(fetchImpl).toHaveBeenCalledTimes(3)
    expect(onUpdate).not.toHaveBeenCalled()
  })

  it('читает версию сборки из meta app-version', () => {
    const doc = document.implementation.createHTMLDocument()
    expect(buildVersion(doc)).toBe('dev')
    const meta = doc.createElement('meta')
    meta.name = 'app-version'
    meta.content = '4bd1e4f'
    doc.head.append(meta)
    expect(buildVersion(doc)).toBe('4bd1e4f')
  })
})
