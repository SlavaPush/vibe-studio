import type {
  ApiError,
  AssetUrl,
  Conversation,
  ConversationList,
  CreateRunRequest,
  MessageList,
  ModelCatalog,
  Run,
  Session,
} from './types'

export class ApiRequestError extends Error {
  readonly error: ApiError
  readonly status: number

  constructor(error: ApiError, status: number) {
    super(error.message)
    this.error = error
    this.status = status
  }
}

const NETWORK_ERROR: ApiError = {
  code: 'service_unavailable',
  message: 'Нет соединения с сервером. Повторите позже.',
  retryable: true,
  operation_id: 'network',
}

const SERVER_ERROR: ApiError = {
  code: 'internal_error',
  message: 'Ошибка сервера. Повторите позже.',
  retryable: false,
  operation_id: 'unknown',
}

function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === 'object' &&
    value !== null &&
    'code' in value &&
    'message' in value &&
    'retryable' in value &&
    'operation_id' in value
  )
}

export function isNetworkError(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 0
}

export function createApiClient(fetchImpl: typeof fetch = (...args) => fetch(...args)) {
  let csrfToken: string | null = null

  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {}
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken
    let response: Response
    try {
      response = await fetchImpl(path, {
        method,
        credentials: 'same-origin',
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      throw new ApiRequestError(NETWORK_ERROR, 0)
    }
    let data: unknown = null
    try {
      data = await response.json()
    } catch {
      if (response.ok) throw new ApiRequestError(NETWORK_ERROR, 0)
    }
    if (!response.ok) {
      throw new ApiRequestError(isApiError(data) ? data : SERVER_ERROR, response.status)
    }
    return data as T
  }

  const remember = (session: Session) => {
    csrfToken = session.csrf_token
    return session
  }

  return {
    models: () => request<ModelCatalog>('GET', '/api/models'),
    me: () => request<Session>('GET', '/api/me').then(remember),
    createSession: (code: string) =>
      request<Session>('POST', '/api/demo-session', { code }).then(remember),
    forgetSession: () => {
      csrfToken = null
    },
    conversations: () => request<ConversationList>('GET', '/api/conversations'),
    createConversation: () => request<Conversation>('POST', '/api/conversations', { title: null }),
    renameConversation: (id: string, title: string) =>
      request<Conversation>('PATCH', `/api/conversations/${id}`, { title }),
    messages: (id: string) => request<MessageList>('GET', `/api/conversations/${id}/messages`),
    createRun: (conversationId: string, body: CreateRunRequest) =>
      request<Run>('POST', `/api/conversations/${conversationId}/runs`, body),
    run: (id: string) => request<Run>('GET', `/api/runs/${id}`),
    cancelRun: (id: string) => request<Run>('POST', `/api/runs/${id}/cancel`),
    assetUrl: (id: string) => request<AssetUrl>('GET', `/api/assets/${id}/url`),
  }
}

export type ApiClient = ReturnType<typeof createApiClient>
