import { describe, expect, it, vi } from 'vitest'
import { RECONNECT_DELAYS_MS, connectOfficeEvents, openTaskEvents } from '@/lib/events'
import { FakeEventSource, installEventSource, taskEvent } from '@/test/fake-api'

const STREAM = '/api/events'
const TASK_STREAM = '/api/tasks/tsk_101/events'

function start(resyncImpl?: () => Promise<void>) {
  installEventSource()
  vi.useFakeTimers()
  const order: string[] = []
  const resync = vi.fn(async () => {
    order.push(`resync:${FakeEventSource.all(STREAM).length}`)
    if (resyncImpl) await resyncImpl()
  })
  const onEvent = vi.fn()
  const onState = vi.fn()
  const events = connectOfficeEvents({ resync, onEvent, onState })
  return { order, resync, onEvent, onState, events }
}

describe('connectOfficeEvents', () => {
  it('перечитывает данные до подключения и повторно после open', async () => {
    const { order, onState, onEvent } = start()
    expect(onState).toHaveBeenLastCalledWith('connecting')
    await vi.advanceTimersByTimeAsync(0)
    expect(order).toEqual(['resync:0'])
    const source = FakeEventSource.last(STREAM)
    source.open()
    expect(onState).not.toHaveBeenCalledWith('open')
    await vi.advanceTimersByTimeAsync(0)
    expect(order).toEqual(['resync:0', 'resync:1'])
    expect(onState).toHaveBeenLastCalledWith('open')
    const event = taskEvent('tsk_1', 'status_changed', { from: 'queued', to: 'developing' })
    source.emit(event)
    expect(onEvent).toHaveBeenCalledWith(event)
  })

  it('после разрыва закрывает поток, перечитывает данные и переподключается с паузами', async () => {
    const { order, onState } = start()
    await vi.advanceTimersByTimeAsync(0)
    for (const [index, delay] of [...RECONNECT_DELAYS_MS, 10000].entries()) {
      const source = FakeEventSource.last(STREAM)
      source.fail()
      expect(source.closed).toBe(true)
      expect(onState).toHaveBeenLastCalledWith('reconnecting')
      await vi.advanceTimersByTimeAsync(delay - 1)
      expect(FakeEventSource.all(STREAM)).toHaveLength(index + 1)
      await vi.advanceTimersByTimeAsync(1)
      expect(FakeEventSource.all(STREAM)).toHaveLength(index + 2)
      expect(order[order.length - 1]).toBe(`resync:${index + 1}`)
    }
    expect(RECONNECT_DELAYS_MS).toEqual([1000, 2000, 5000, 10000])
  })

  it('успешное подключение и перечитывание сбрасывают паузу', async () => {
    start()
    await vi.advanceTimersByTimeAsync(0)
    FakeEventSource.last(STREAM).fail()
    await vi.advanceTimersByTimeAsync(1000)
    FakeEventSource.last(STREAM).fail()
    await vi.advanceTimersByTimeAsync(2000)
    FakeEventSource.last(STREAM).open()
    await vi.advanceTimersByTimeAsync(0)
    FakeEventSource.last(STREAM).fail()
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeEventSource.all(STREAM)).toHaveLength(4)
  })

  it('ошибка перечитывания до подключения не открывает поток и повторяется с паузой', async () => {
    let fail = true
    const { onState, resync } = start(async () => {
      if (fail) throw new Error('500')
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeEventSource.all(STREAM)).toHaveLength(0)
    expect(onState).toHaveBeenLastCalledWith('reconnecting')
    fail = false
    await vi.advanceTimersByTimeAsync(1000)
    expect(resync).toHaveBeenCalledTimes(2)
    expect(FakeEventSource.all(STREAM)).toHaveLength(1)
  })

  it('ошибка перечитывания после open закрывает поток и повторяет восстановление', async () => {
    let calls = 0
    const { onState, resync } = start(async () => {
      calls += 1
      if (calls === 2) throw new Error('500')
    })
    await vi.advanceTimersByTimeAsync(0)
    const first = FakeEventSource.last(STREAM)
    first.open()
    await vi.advanceTimersByTimeAsync(0)
    expect(first.closed).toBe(true)
    expect(onState).not.toHaveBeenCalledWith('open')
    expect(onState).toHaveBeenLastCalledWith('reconnecting')
    await vi.advanceTimersByTimeAsync(1000)
    expect(resync).toHaveBeenCalledTimes(3)
    FakeEventSource.last(STREAM).open()
    await vi.advanceTimersByTimeAsync(0)
    expect(onState).toHaveBeenLastCalledWith('open')
  })

  it('recover после синхронизации перечитывает данные и переподключается', async () => {
    const { events, resync } = start()
    events.recover()
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeEventSource.all(STREAM)).toHaveLength(1)
    FakeEventSource.last(STREAM).open()
    await vi.advanceTimersByTimeAsync(0)
    expect(resync).toHaveBeenCalledTimes(2)
    events.recover()
    expect(FakeEventSource.last(STREAM).closed).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(resync).toHaveBeenCalledTimes(3)
    expect(FakeEventSource.all(STREAM)).toHaveLength(2)
  })

  it('recover во время перечитывания после open повторяет перечитывание до состояния open', async () => {
    const pending: Array<() => void> = []
    const { events, resync, onState } = start(
      () =>
        new Promise<void>((resolve) => {
          pending.push(resolve)
        }),
    )
    await vi.advanceTimersByTimeAsync(0)
    pending.shift()?.()
    await vi.advanceTimersByTimeAsync(0)
    const source = FakeEventSource.last(STREAM)
    source.open()
    await vi.advanceTimersByTimeAsync(0)
    expect(resync).toHaveBeenCalledTimes(2)
    events.recover()
    pending.shift()?.()
    await vi.advanceTimersByTimeAsync(0)
    expect(resync).toHaveBeenCalledTimes(3)
    expect(onState).not.toHaveBeenCalledWith('open')
    expect(source.closed).toBe(false)
    pending.shift()?.()
    await vi.advanceTimersByTimeAsync(0)
    expect(onState).toHaveBeenLastCalledWith('open')
    expect(resync).toHaveBeenCalledTimes(3)
  })

  it('остановка закрывает поток и отменяет переподключение', async () => {
    const { events, resync } = start()
    await vi.advanceTimersByTimeAsync(0)
    FakeEventSource.last(STREAM).fail()
    events.stop()
    await vi.advanceTimersByTimeAsync(20000)
    expect(FakeEventSource.all(STREAM)).toHaveLength(1)
    expect(resync).toHaveBeenCalledTimes(1)
  })

  it('не подключается, если остановлен во время перечитывания', async () => {
    installEventSource()
    const events = connectOfficeEvents({
      resync: () => Promise.resolve(),
      onEvent: vi.fn(),
      onState: vi.fn(),
    })
    events.stop()
    await Promise.resolve()
    expect(FakeEventSource.all(STREAM)).toHaveLength(0)
  })
})

describe('openTaskEvents', () => {
  function open() {
    installEventSource()
    vi.useFakeTimers()
    const onEvent = vi.fn()
    const onState = vi.fn()
    const onBroken = vi.fn(() => Promise.resolve())
    const stop = openTaskEvents('tsk_101', { onEvent, onState, onBroken })
    return { onEvent, onState, onBroken, stop }
  }

  it('подключает поток задачи и закрывает его', () => {
    const { onEvent, onState, stop } = open()
    const source = FakeEventSource.last(TASK_STREAM)
    source.open()
    expect(onState).toHaveBeenLastCalledWith('open')
    const event = taskEvent('tsk_101', 'role_started', { step: 'spec' })
    source.emit(event)
    expect(onEvent).toHaveBeenCalledWith(event)
    stop()
    expect(source.closed).toBe(true)
  })

  it('переподключение браузером не создаёт новый поток', async () => {
    const { onState, onBroken } = open()
    FakeEventSource.last(TASK_STREAM).fail('reconnecting')
    expect(onState).toHaveBeenLastCalledWith('reconnecting')
    await vi.advanceTimersByTimeAsync(20000)
    expect(onBroken).not.toHaveBeenCalled()
    expect(FakeEventSource.all(TASK_STREAM)).toHaveLength(1)
  })

  it('закрытый браузером поток проверяет сеанс и открывается заново', async () => {
    const { onBroken, stop } = open()
    FakeEventSource.last(TASK_STREAM).fail('closed')
    expect(onBroken).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeEventSource.all(TASK_STREAM)).toHaveLength(2)
    FakeEventSource.last(TASK_STREAM).fail('closed')
    stop()
    await vi.advanceTimersByTimeAsync(20000)
    expect(FakeEventSource.all(TASK_STREAM)).toHaveLength(2)
  })
})
