import { describe, expect, it } from 'vitest'
import { contractEnums, contractExamples } from '../../../src/mocks/contract-examples'
import { fixtures, mockApi, type MockOverrides } from './mock-api'

interface Fulfilled {
  status: number
  contentType: string
  body: string
}

type RouteHandler = (route: unknown) => Promise<void>

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function differences(actual: unknown, expected: unknown, path = '$'): string[] {
  if (!isObject(actual) || !isObject(expected)) return []
  const found = Object.keys(actual).sort().join(', ')
  const wanted = Object.keys(expected).sort().join(', ')
  if (found !== wanted) return [`${path}: ${found} != ${wanted}`]
  return Object.keys(expected).flatMap((key) => differences(actual[key], expected[key], `${path}.${key}`))
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

function identifiers(value: unknown, path = '$'): [string, unknown][] {
  if (Array.isArray(value)) return value.flatMap((item, index) => identifiers(item, `${path}[${index}]`))
  if (!isObject(value)) return []
  return Object.entries(value).flatMap(([key, item]) => [
    ...((key === 'id' || key.endsWith('_id')) && key !== 'operation_id' && item !== null
      ? [[`${path}.${key}`, item] as [string, unknown]]
      : []),
    ...identifiers(item, `${path}.${key}`),
  ])
}

function expectShape(actual: unknown, expected: unknown) {
  expect(isObject(actual)).toBe(true)
  expect(differences(actual, expected)).toEqual([])
  expect(identifiers(actual).filter(([, id]) => typeof id !== 'string' || !UUID.test(id))).toEqual([])
}

async function api(overrides?: MockOverrides) {
  let handler: RouteHandler | null = null
  const page = {
    route: async (_pattern: string, callback: RouteHandler) => {
      handler = callback
    },
  }
  await mockApi(page as unknown as Parameters<typeof mockApi>[0], overrides)
  return async (method: string, path: string, body?: unknown): Promise<Fulfilled> => {
    let result: Fulfilled | null = null
    await handler!({
      request: () => ({
        url: () => `http://127.0.0.1:4173${path}`,
        method: () => method,
        postDataJSON: () => body ?? null,
        postData: () => (body === undefined ? null : JSON.stringify(body)),
      }),
      fulfill: async (reply: Fulfilled) => {
        result = reply
      },
    })
    return result!
  }
}

function json(reply: Fulfilled): unknown {
  expect(reply.contentType).toBe('application/json')
  return JSON.parse(reply.body)
}

function streamEvents(body: string): Record<string, unknown>[] {
  return body
    .trim()
    .split('\n\n')
    .map((block) => {
      const lines = Object.fromEntries(block.split('\n').map((line) => [line.split(': ')[0], line.slice(line.indexOf(': ') + 2)]))
      const event = JSON.parse(lines.data) as Record<string, unknown>
      expect(lines.event).toBe(event.type)
      expect(lines.id).toBe(String(event.seq))
      return event
    })
}

describe('mockApi', () => {
  it('отвечает на запросы сеанса, каталога и диалогов по контракту', async () => {
    const call = await api()
    expect(json(await call('GET', '/api/me'))).toEqual(contractExamples.Session[0])
    expect(json(await call('GET', '/api/models'))).toEqual(contractExamples.ModelCatalog[0])
    expectShape(json(await call('GET', '/api/version')), contractExamples.VersionInfo[0])
    const list = json(await call('GET', '/api/conversations')) as { items: unknown[] }
    expectShape(list, contractExamples.ConversationList[0])
    expect(list.items).toEqual([fixtures.conversation])
    expectShape(fixtures.conversation, contractExamples.ConversationList[0].items[0])
    const created = await call('POST', '/api/conversations', { title: null })
    expect(created.status).toBe(201)
    expectShape(json(created), contractExamples.Conversation[0])
    const renamed = json(await call('PATCH', `/api/conversations/${fixtures.conversation.id}`, { title: 'Другое' }))
    expect(renamed).toEqual({ ...fixtures.conversation, title: 'Другое' })
    const messages = json(await call('GET', `/api/conversations/${fixtures.conversation.id}/messages`))
    expectShape(messages, contractExamples.MessageList[0])
  })

  it('название диалога не входит в другие названия контракта', () => {
    const titles = contractExamples.ConversationList[0].items.map((item) => item.title)
    expect(titles.some((title) => title.includes(fixtures.conversation.title))).toBe(false)
  })

  it('создаёт задание и передаёт поток событий до ответа ассистента', async () => {
    const call = await api()
    const created = await call('POST', `/api/conversations/${fixtures.conversation.id}/runs`, {
      text: 'Привет',
      model: 'auto',
      idempotency_key: '6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d',
    })
    expect(created.status).toBe(202)
    const run = json(created) as Record<string, string>
    expectShape(run, contractExamples.Run[1])
    expect(run).toMatchObject({ status: 'queued', conversation_id: fixtures.conversation.id, requested_model: 'auto' })

    const stream = await call('GET', `/api/runs/${run.id}/events`)
    expect(stream.status).toBe(200)
    expect(stream.contentType).toBe('text/event-stream')
    const events = streamEvents(stream.body)
    expect(events.map((event) => event.type)).toEqual([
      'accepted',
      'checks_passed',
      'model_selected',
      'text_delta',
      'completed',
    ])
    for (const event of events) {
      expect(event.run_id).toBe(run.id)
      expect(contractEnums.RunEventType).toHaveProperty(String(event.type))
      const example = contractExamples.RunEvent.find((item) => item.type === event.type)
      expectShape(event, example)
    }
    const checks = events[1].payload as Record<string, string>
    expect(contractEnums.Intent).toHaveProperty(checks.intent)
    expect(contractEnums.Complexity).toHaveProperty(checks.complexity)
    expect(contractEnums.Risk).toHaveProperty(checks.risk)
    const completed = events[4].payload as { message: Record<string, unknown> }
    expect(completed.message).toMatchObject({ role: 'assistant', text: fixtures.reply, conversation_id: fixtures.conversation.id })
    expect(contractEnums.ModelId).toHaveProperty(String(completed.message.effective_model))
    expect(contractEnums.Effort).toHaveProperty(String(completed.message.effort))

    const finished = json(await call('GET', `/api/runs/${run.id}`)) as Record<string, string>
    expectShape(finished, contractExamples.Run[0])
    expect(finished.status).toBe('completed')
    expect(finished.assistant_message_id).toBe(completed.message.id)

    const second = json(
      await call('POST', `/api/conversations/${fixtures.conversation.id}/runs`, {
        text: 'Ещё',
        model: 'auto',
        idempotency_key: '7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e',
      }),
    ) as Record<string, string>
    expect(second.id).not.toBe(run.id)
    const secondEvents = streamEvents((await call('GET', `/api/runs/${second.id}/events`)).body)
    const eventIds = [...events, ...secondEvents].map((event) => String(event.event_id))
    expect(eventIds.every((id) => UUID.test(id))).toBe(true)
    expect(new Set(eventIds).size).toBe(eventIds.length)
  })

  it('отвечает 404 по контракту на неподменённый путь', async () => {
    const call = await api()
    for (const [method, path] of [
      ['GET', '/api/unknown'],
      ['DELETE', '/api/conversations'],
      ['GET', '/api/runs/missing/events'],
    ]) {
      const reply = await call(method, path)
      expect(reply.status).toBe(404)
      const error = json(reply) as { code: string }
      expectShape(error, contractExamples.ApiError[0])
      expect(error.code).toBe('not_found')
      expect(contractEnums.ErrorCode).toHaveProperty(error.code)
    }
  })

  it('применяет overrides по методу и пути', async () => {
    const call = await api({
      'GET /api/conversations': { body: { items: [] } },
      'GET /api/conversations/{id}/messages': ({ params }) => ({ status: 404, body: { id: params.id } }),
      'GET /api/me': { status: 401, body: contractExamples.ApiError[2] },
    })
    expect(json(await call('GET', '/api/conversations'))).toEqual({ items: [] })
    const messages = await call('GET', '/api/conversations/abc/messages')
    expect([messages.status, json(messages)]).toEqual([404, { id: 'abc' }])
    const me = await call('GET', '/api/me')
    expect(me.status).toBe(401)
    expect(json(await call('GET', '/api/models'))).toEqual(contractExamples.ModelCatalog[0])
  })
})
