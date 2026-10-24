import type { Page, Request } from '@playwright/test'

export interface MockReply {
  status?: number
  body?: unknown
  contentType?: string
  headers?: Record<string, string>
}

export interface MockRequest {
  method: string
  path: string
  params: Record<string, string>
  query: URLSearchParams
  body: unknown
  request: Request
}

export type MockHandler = MockReply | ((request: MockRequest) => MockReply | Promise<MockReply>)
export type MockOverrides = Record<string, MockHandler>

const conversation = {
  id: '5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37',
  title: 'Тестовый диалог QA',
  created_at: '2026-10-23T18:29:40Z',
  updated_at: '2026-10-23T18:30:12Z',
}

const newConversation = {
  id: '9a3b7c11-2e4f-4d6a-8b9c-0e1f2a3b4c5d',
  title: 'Новый диалог',
  created_at: '2026-10-23T17:05:00Z',
  updated_at: '2026-10-23T17:05:00Z',
}

export const fixtures = {
  session: {
    user_id: '7b0c2f3e-5d1a-4c8e-9f21-3a6d8e4b1c90',
    expires_at: '2026-10-30T18:00:00Z',
    csrf_token: 'b4f1c9e27d3a48e6a0c5f8d1e2b3a4c5',
  },
  models: {
    items: [
      { id: 'claude-haiku-5-5', provider: 'anthropic', label: 'Claude Haiku 5.5', kind: 'text', selectable: true },
      { id: 'claude-sonnet-5-5', provider: 'anthropic', label: 'Claude Sonnet 5.5', kind: 'text', selectable: true },
      { id: 'gpt-6-luna', provider: 'openai', label: 'GPT-6 Luna', kind: 'text', selectable: true },
      { id: 'gpt-6.1-sol', provider: 'openai', label: 'GPT-6.1 Sol', kind: 'text', selectable: true },
      { id: 'gpt-image-2.5-flare', provider: 'openai', label: 'GPT Image 2.5 Flare', kind: 'image', selectable: false },
    ],
  },
  version: { version: 'dev' },
  conversation,
  newConversation,
  conversations: { items: [conversation] },
  messages: { items: [], active_run_id: null, runs: [] },
  reply: 'Тестовый ответ ассистента',
  model: 'claude-sonnet-5-5',
}

type RunRecord = Record<string, unknown>

const NOW = '2026-10-23T18:30:00Z'

function apiError(code: string, message: string) {
  return { code, message, retryable: false, operation_id: 'op_office_mock' }
}

function notFound(): MockReply {
  return { status: 404, body: apiError('not_found', 'Объект не найден.') }
}

function message(run: Record<string, unknown>, id: string, role: string, text: string, model: string | null) {
  return {
    id,
    conversation_id: run.conversation_id,
    role,
    kind: null,
    text,
    run_id: run.id,
    effective_model: model,
    effort: model ? 'high' : null,
    asset: null,
    created_at: NOW,
  }
}

function events(run: RunRecord): string {
  const assistant = message(run, String(run.assistant_message_id), 'assistant', fixtures.reply, fixtures.model)
  const items: [string, Record<string, unknown>][] = [
    ['accepted', { user_message_id: run.user_message_id, requested_model: run.requested_model }],
    ['checks_passed', { intent: 'chat', complexity: 'standard', risk: 'none' }],
    [
      'model_selected',
      { assistant_message_id: assistant.id, provider: 'anthropic', effective_model: fixtures.model, effort: 'high' },
    ],
    ['text_delta', { message_id: assistant.id, delta: fixtures.reply }],
    ['completed', { message: assistant }],
  ]
  return items
    .map(([type, payload], index) => {
      const event = {
        event_id: `7d2e9f10-${String(index + 1).padStart(4, '0')}-4b3c-9d4e-${String(run.id).slice(-12)}`,
        run_id: run.id,
        seq: index + 1,
        type,
        timestamp: NOW,
        payload,
      }
      return `id: ${index + 1}\nevent: ${type}\ndata: ${JSON.stringify(event)}\n\n`
    })
    .join('')
}

function pattern(template: string): RegExp {
  const source = template
    .split('/')
    .map((part) => (/^\{\w+\}$/.test(part) ? `(?<${part.slice(1, -1)}>[^/]+)` : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    .join('/')
  return new RegExp(`^${source}$`)
}

function find(handlers: MockOverrides, method: string, path: string) {
  for (const [key, handler] of Object.entries(handlers)) {
    const [keyMethod, template] = key.split(' ')
    if (keyMethod !== method || !template) continue
    const found = pattern(template).exec(path)
    if (found) return { handler, params: { ...found.groups } }
  }
  return null
}

export async function mockApi(page: Page, overrides: MockOverrides = {}): Promise<void> {
  const runs = new Map<string, RunRecord>()
  let counter = 0

  const defaults: MockOverrides = {
    'GET /api/me': { body: fixtures.session },
    'POST /api/demo-session': { status: 201, body: fixtures.session },
    'GET /api/models': { body: fixtures.models },
    'GET /api/version': { body: fixtures.version },
    'GET /api/conversations': { body: fixtures.conversations },
    'POST /api/conversations': { status: 201, body: fixtures.newConversation },
    'PATCH /api/conversations/{id}': ({ params, body }) => {
      const known = [fixtures.conversation, fixtures.newConversation].find((item) => item.id === params.id)
      if (!known) return notFound()
      const title = (body as { title?: unknown } | null)?.title
      return { body: { ...known, title: typeof title === 'string' ? title : known.title } }
    },
    'GET /api/conversations/{id}/messages': { body: fixtures.messages },
    'POST /api/conversations/{id}/runs': ({ params, body }) => {
      counter += 1
      const request = (body ?? {}) as { model?: unknown }
      const id = `0b8d6f24-7a3e-4c91-b2d5-${String(counter).padStart(12, '0')}`
      const run = {
        id,
        conversation_id: params.id,
        status: 'queued',
        requested_model: typeof request.model === 'string' ? request.model : 'auto',
        effective_model: null,
        effort: null,
        intent: null,
        complexity: null,
        risk: null,
        switch_reason: null,
        user_message_id: `1c2d3e4f-5a6b-4c7d-8e9f-${String(counter).padStart(12, '0')}`,
        assistant_message_id: null,
        error: null,
        created_at: NOW,
        updated_at: NOW,
      }
      runs.set(id, { ...run, assistant_message_id: `3e4f5a6b-7c8d-4e9f-8a1b-${String(counter).padStart(12, '0')}` })
      return { status: 202, body: run }
    },
    'GET /api/runs/{id}': ({ params }) => {
      const record = runs.get(params.id)
      if (!record) return notFound()
      return {
        body: {
          ...record,
          status: 'completed',
          effective_model: fixtures.model,
          effort: 'high',
          intent: 'chat',
          complexity: 'standard',
          risk: 'none',
        },
      }
    },
    'GET /api/runs/{id}/events': ({ params }) => {
      const record = runs.get(params.id)
      if (!record) return notFound()
      return { contentType: 'text/event-stream', headers: { 'cache-control': 'no-store' }, body: events(record) }
    },
  }

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const found = find(overrides, method, url.pathname) ?? find(defaults, method, url.pathname)
    let body: unknown = null
    try {
      body = request.postDataJSON()
    } catch {
      body = request.postData()
    }
    const reply = !found
      ? notFound()
      : typeof found.handler === 'function'
        ? await found.handler({ method, path: url.pathname, params: found.params, query: url.searchParams, body, request })
        : found.handler
    const contentType = reply.contentType ?? 'application/json'
    await route.fulfill({
      status: reply.status ?? 200,
      contentType,
      headers: reply.headers,
      body:
        typeof reply.body === 'string' && contentType !== 'application/json'
          ? reply.body
          : JSON.stringify(reply.body ?? null),
    })
  })
}
