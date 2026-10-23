import type {
  ApiError,
  CheckReport,
  Decision,
  OfficeSessionRequest,
  Participant,
  ReleaseCandidate,
  ReleaseDecisionRequest,
  Slots,
  Task,
  TaskCreate,
  TaskDecisionRequest,
} from '@/api/types'

export const NETWORK_MESSAGE = 'API офиса недоступен. Проверьте соединение и повторите.'
export const SERVER_MESSAGE = 'Внутренняя ошибка API офиса. Проверьте состояние объекта перед повтором.'
export const STALE_MESSAGE = 'Сеанс изменился, ответ не применён.'

export class ApiRequestError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly retryAfter: number | null

  constructor(
    status: number,
    code: string,
    message: string,
    retryable = false,
    retryAfter: number | null = null,
  ) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.code = code
    this.retryable = retryable
    this.retryAfter = retryAfter
  }
}

export type Run = <T>(operation: () => Promise<T>) => Promise<T>

export function asApiError(error: unknown): ApiRequestError {
  return error instanceof ApiRequestError ? error : new ApiRequestError(0, 'network', NETWORK_MESSAGE)
}

export function isStale(error: unknown): boolean {
  return error instanceof ApiRequestError && error.code === 'stale'
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 401 && error.code !== 'invalid_access_code'
}

function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value.trim())) return null
  return Number(value.trim())
}

async function toError(response: Response): Promise<ApiRequestError> {
  let body: Partial<ApiError> = {}
  try {
    body = (await response.json()) as Partial<ApiError>
  } catch {
    body = {}
  }
  const code = typeof body.code === 'string' ? body.code : 'internal_error'
  const message =
    response.status >= 500 || typeof body.message !== 'string' ? SERVER_MESSAGE : body.message
  return new ApiRequestError(
    response.status,
    code,
    message,
    body.retryable === true,
    parseRetryAfter(response.headers.get('Retry-After')),
  )
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response
  try {
    response = await fetch(path, { ...init, credentials: 'same-origin' })
  } catch {
    throw new ApiRequestError(0, 'network', NETWORK_MESSAGE)
  }
  if (!response.ok) throw await toError(response)
  return response
}

async function readBody<T>(read: () => Promise<T>): Promise<T> {
  try {
    return await read()
  } catch {
    throw new ApiRequestError(0, 'network', NETWORK_MESSAGE)
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path, { headers: { Accept: 'application/json' } })
  return readBody(() => response.json() as Promise<T>)
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return readBody(() => response.json() as Promise<T>)
}

const taskPath = (id: string) => `/api/tasks/${encodeURIComponent(id)}`

export const me = () => getJson<Participant>('/api/me')

export const login = (body: OfficeSessionRequest) =>
  postJson<Participant>('/api/office-session', body)

export const listTasks = () => getJson<Task[]>('/api/tasks')

export const getTask = (id: string) => getJson<Task>(taskPath(id))

export const createTask = (body: TaskCreate) => postJson<Task>('/api/tasks', body)

export async function taskDiff(id: string): Promise<string> {
  const response = await request(`${taskPath(id)}/diff`, { headers: { Accept: 'text/plain' } })
  return readBody(() => response.text())
}

export const taskChecks = (id: string) => getJson<CheckReport>(`${taskPath(id)}/checks`)

export const decideTask = (id: string, body: TaskDecisionRequest) =>
  postJson<Decision>(`${taskPath(id)}/decisions`, body)

export const listReleases = () => getJson<ReleaseCandidate[]>('/api/releases')

export const decideRelease = (id: string, body: ReleaseDecisionRequest) =>
  postJson<Decision>(`/api/releases/${encodeURIComponent(id)}/decisions`, body)

export const getSlots = () => getJson<Slots>('/api/slots')
