import type { AgentRole, PullRequestCheck, ReleaseCandidate, Task, TaskEvent } from '@/api/types'
import {
  AGENT_ROLE_LABELS,
  CHECK_STATUS_LABELS,
  DECISION_LABELS,
  EVENT_TYPE_LABELS,
  RELEASE_STATUS_LABELS,
  RENDER_STATUS_LABELS,
  REVIEW_VERDICT_LABELS,
  STATUS_LABELS,
} from '@/lib/labels'

export type AgentKey = 'intake' | AgentRole

export const AGENT_LABELS: Record<AgentKey, string> = { intake: 'Фильтр приёма', ...AGENT_ROLE_LABELS }

export type AgentState = 'waiting' | 'working' | 'done' | 'blocked' | 'error' | 'stopped' | 'skipped'

export const AGENT_STATE_LABELS: Record<AgentState, string> = {
  waiting: 'ожидает',
  working: 'работает',
  done: 'завершил',
  blocked: 'завершил с замечаниями',
  error: 'ошибка',
  stopped: 'остановлен',
  skipped: 'не понадобился',
}

export interface Phrase {
  text: string
  code?: string
  title?: string
  tail?: string
}

export interface ModelInfo {
  provider: string | null
  model: string
  effort: string | null
}

export interface AgentView {
  key: AgentKey
  label: string
  state: AgentState
  stateLabel: string
  model: ModelInfo | null
  activity: Phrase | null
  note: string | null
}

export type HandoffKey = 'task' | 'questions' | 'requirements' | 'findings' | 'code' | 'reports'

export interface Handoff {
  key: HandoffKey
  from: AgentKey[]
  to: AgentKey[]
  label: string
  active: boolean
}

export type StageState = 'waiting' | 'running' | 'done' | 'error'

export const STAGE_STATE_LABELS: Record<StageState, string> = {
  waiting: 'ожидает',
  running: 'выполняется',
  done: 'готово',
  error: 'ошибка',
}

export interface StageLink {
  href: string
  label: string
}

export interface CheckLine {
  name: string
  ok: boolean
}

export interface Stage {
  key: string
  title: string
  state: StageState
  detail: Phrase | null
  link: StageLink | null
  checks: PullRequestCheck[]
  results: CheckLine[]
}

export interface Pipeline {
  agents: Record<AgentKey, AgentView>
  handoffs: Handoff[]
  github: Stage[]
  release: Stage[]
  releaseStarted: boolean
  rework: { attempt: number; limit: number } | null
}

type Events = readonly TaskEvent[]
type Match = (event: TaskEvent) => boolean

const STOP_STATUSES = new Set(['failed', 'needs_attention', 'cancelled', 'queued'])
const FAILED_RENDER = new Set(['build_failed', 'update_failed', 'canceled', 'deactivated'])
const passed = (check: PullRequestCheck) => check.status === 'completed' && check.conclusion === 'success'
const FINISHED = new Set<AgentState>(['done', 'blocked'])

const STEP_ACTIVITY: Record<AgentRole, string> = {
  supervisor: 'составляет требования',
  researcher: 'изучает публичные источники',
  developer: 'пишет код',
  qa: 'пишет тест Playwright',
  critic: 'сверяет изменения с требованиями',
  pr_reviewer: 'проверяет PR',
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function short(sha: string): string {
  return sha.slice(0, 7)
}

const isRevert: Match = (event) => 'revert_of' in event.payload

function lastIndex(events: Events, match: Match, from = 0, until = events.length): number {
  for (let index = until - 1; index >= Math.max(from, 0); index -= 1) {
    if (match(events[index])) return index
  }
  return -1
}

function at(events: Events, index: number): TaskEvent | null {
  return index >= 0 ? events[index] : null
}

const isStop: Match = (event) => {
  if (isRevert(event)) return false
  if (event.type === 'rework_started') return true
  if (event.type === 'status_changed') return STOP_STATUSES.has(String(event.payload.to))
  return (
    event.type === 'role_finished' &&
    event.role === null &&
    (event.payload.outcome === 'blocked' || event.payload.outcome === 'error')
  )
}

function stopNote(event: TaskEvent): string {
  if (event.type === 'role_finished') return text(event.payload.summary) ?? 'Работа остановлена.'
  if (event.type === 'rework_started') return 'Задача возвращена на доработку.'
  return `Статус задачи: ${statusLabel(event.payload.to)}.`
}

function statusLabel(value: unknown): string {
  const key = String(value)
  return (STATUS_LABELS as Record<string, string | undefined>)[key] ?? key
}

export function modelOf(event: TaskEvent): ModelInfo | null {
  const model = text(event.payload.model)
  if (model) return { provider: text(event.payload.provider), model, effort: text(event.payload.effort) }
  const target = text(event.payload.arguments)?.split(' ')[1]
  if (!target) return null
  const slash = target.indexOf('/')
  if (slash <= 0) return { provider: null, model: target, effort: null }
  return { provider: target.slice(0, slash), model: target.slice(slash + 1), effort: null }
}

function fileName(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path
}

function host(url: string): string {
  try {
    return new URL(url).host || url
  } catch {
    return url
  }
}

function checkPhrase(event: TaskEvent): Phrase {
  const step = text(event.payload.step) ?? text(event.payload.command) ?? 'проверка'
  const ok = event.payload.exit_code === 0
  return { text: 'проверка ', code: step, tail: ok ? ': успешно' : ': ошибка' }
}

function toolPhrase(event: TaskEvent, role: AgentRole | null): Phrase {
  const args = text(event.payload.arguments) ?? ''
  const result = event.payload.result
  let phrase: Phrase
  switch (event.tool) {
    case 'read_file':
      phrase = { text: 'читает ', code: fileName(args), title: args }
      break
    case 'write_file': {
      const path = args.replace(/ \([^)]*\)$/, '')
      phrase = { text: 'изменяет ', code: fileName(path), title: path }
      break
    }
    case 'delete_file':
      phrase = { text: 'удаляет ', code: fileName(args), title: args }
      break
    case 'list_dir':
      phrase = { text: 'просматривает каталог ', code: args || '.' }
      break
    case 'fetch_public':
      phrase = { text: 'читает страницу ', code: host(args), title: args }
      break
    case 'run_check':
      return result === 'denied' ? { text: 'проверка запрещена политикой' } : { text: 'запускает проверку ', code: args }
    case 'model':
      phrase = { text: role === 'qa' ? 'пишет тест Playwright' : 'обращается к модели' }
      break
    case 'git_commit':
      phrase = { text: 'фиксирует изменения' }
      break
    case 'git_push':
      phrase = { text: 'отправляет ветку ', code: args.split(' ')[0] }
      break
    case 'git_revert':
      phrase = { text: 'готовит revert-commit' }
      break
    case 'rework_materials':
      phrase = { text: 'собирает материалы доработки' }
      break
    default:
      phrase = { text: 'вызывает инструмент ', code: event.tool ?? 'нет данных' }
  }
  if (result === 'denied') return { ...phrase, tail: `${phrase.tail ?? ''} — отказ` }
  if (result === 'error') return { ...phrase, tail: `${phrase.tail ?? ''} — ошибка` }
  return phrase
}

function modelPhrase(event: TaskEvent): Phrase {
  const info = modelOf(event)
  if (!info) return { text: 'обращается к модели' }
  return {
    text: 'обращается к модели ',
    code: info.provider ? `${info.provider}/${info.model}` : info.model,
    tail: info.effort ? `, усилие ${info.effort}` : undefined,
  }
}

function finishedPhrase(event: TaskEvent): Phrase {
  const summary = text(event.payload.summary)
  const outcome = event.payload.outcome
  if (event.role === null && event.payload.step === 'release') {
    return { text: `Выпуск: ${summary ?? String(outcome)}` }
  }
  if (event.role === null && outcome !== 'ok') return { text: `Остановка: ${summary ?? 'нет данных'}` }
  const head = outcome === 'ok' ? 'завершил работу' : outcome === 'blocked' ? 'завершил с замечаниями' : 'ошибка'
  return { text: summary ? `${head}: ${summary}` : head }
}

function checksSummary(checks: PullRequestCheck[]): string {
  return `${checks.filter(passed).length} из ${checks.length} успешно`
}

function checksOf(event: TaskEvent | null): PullRequestCheck[] {
  const checks = event?.payload.checks
  return Array.isArray(checks) ? (checks as PullRequestCheck[]) : []
}

function decisionLabel(value: unknown): string {
  const key = String(value)
  return (DECISION_LABELS as Record<string, string | undefined>)[key] ?? key
}

function renderLabel(value: unknown): string {
  const key = String(value)
  return RENDER_STATUS_LABELS[key] ?? key
}

export function describeEvent(event: TaskEvent): Phrase {
  const { payload } = event
  const prefix = isRevert(event) ? 'revert-PR' : 'PR'
  switch (event.type) {
    case 'task_created':
      return { text: 'Задача создана' }
    case 'task_rejected':
      return { text: `Фильтр приёма отклонил задачу: ${text(payload.reason) ?? 'нет данных'}` }
    case 'status_changed': {
      const label = statusLabel(payload.to)
      const reason = text(payload.reason)
      return { text: reason ? `Статус: ${label}. Причина: ${reason}` : `Статус: ${label}` }
    }
    case 'role_started':
      return { text: 'начал работу' }
    case 'role_finished':
      return finishedPhrase(event)
    case 'tool_called':
      return event.tool === 'model' ? modelPhrase(event) : toolPhrase(event, event.role)
    case 'artifact_ready':
      return { text: 'Артефакт готов: ', code: text(payload.name) ?? event.artifact ?? 'нет данных' }
    case 'decision_required':
      return { text: `Требуется решение: ${decisionLabel(payload.decision)}` }
    case 'decision_recorded':
      return { text: `Решение записано: ${decisionLabel(payload.decision)}` }
    case 'check_result': {
      const phrase = checkPhrase(event)
      return { ...phrase, text: 'Проверка ' }
    }
    case 'release_candidate':
      return { text: 'Создан кандидат выпуска ', code: short(String(payload.commit_sha ?? '')) }
    case 'deploy_status':
      if ('verified' in payload) {
        if (payload.fault === true) return { text: 'Проверка после деплоя: управляемый сбой' }
        return { text: `Проверка после деплоя: ${payload.verified === true ? 'пройдена' : 'не пройдена'}` }
      }
      return { text: `Деплой Render: ${renderLabel(payload.render_status)}` }
    case 'pr_opened':
      return { text: `${prefix} #${String(payload.pr_number)} открыт` }
    case 'checks_status':
      return {
        text: `Обязательные проверки: ${checksSummary(checksOf(event))} для `,
        code: short(String(payload.head_sha ?? '')),
      }
    case 'pr_closed':
      return { text: `${prefix} #${String(payload.pr_number)} закрыт` }
    case 'operation_unknown':
      return { text: `Исход операции неизвестен: ${text(payload.reason) ?? String(payload.kind)}` }
    case 'pr_review':
      return { text: `Агент ревью: ${prefix} ${payload.verdict === 'approve' ? 'принят' : 'отклонён'}` }
    case 'merge_started':
      return { text: `Слияние ${prefix} #${String(payload.pr_number)} начато` }
    case 'pr_merged':
      return { text: `${prefix} #${String(payload.pr_number)} слит в main, commit `, code: short(String(payload.merge_sha ?? '')) }
    case 'rollback_status':
      return { text: `Откат Render: ${renderLabel(payload.render_status)}` }
    case 'release_blocked':
      return { text: `Выпуск заблокирован: ${text(payload.reason) ?? 'нет данных'}` }
    case 'release_unblocked':
      return { text: 'Блокировка выпуска снята' }
    case 'external_publish':
      return { text: 'Опубликована версия вне офиса ', code: short(String(payload.published_sha ?? '')) }
    case 'rework_started':
      return { text: `Доработка после отката: попытка ${String(payload.attempt)} из ${String(payload.limit)}` }
    default:
      return { text: 'Событие ', code: event.type }
  }
}

export interface DetailLine {
  label: string
  value: string
  href?: string
}

const DETAIL_LABELS: Record<string, string> = {
  title: 'Название',
  change_class: 'Класс задачи',
  profile: 'Профиль',
  provider: 'Провайдер',
  model: 'Модель',
  effort: 'Усилие',
  reason: 'Причина',
  from: 'Было',
  to: 'Стало',
  step: 'Шаг',
  outcome: 'Итог',
  summary: 'Итог словами агента',
  arguments: 'Аргументы',
  result: 'Результат',
  name: 'Имя',
  size_bytes: 'Размер, байт',
  decision: 'Решение',
  decision_id: 'Идентификатор решения',
  participant_id: 'Участник',
  expected_version: 'Версия задачи',
  command: 'Команда',
  exit_code: 'Код выхода',
  duration_ms: 'Длительность',
  commit_sha: 'Commit',
  qa_sha: 'Commit теста QA',
  release_id: 'Выпуск',
  render_deploy_id: 'Деплой Render',
  render_status: 'Статус Render',
  verified: 'Проверка после деплоя',
  fault: 'Управляемый сбой',
  target_sha: 'Прежняя версия',
  pr_number: 'PR',
  pr_url: 'Ссылка на PR',
  head_sha: 'Head',
  merge_sha: 'Commit слияния',
  review_id: 'Решение агента ревью',
  verdict: 'Вердикт',
  reasons: 'Обоснование',
  kind: 'Операция',
  blocked_by: 'Блокировка выпуска',
  fixed_by: 'Исправляющий выпуск',
  published_sha: 'Опубликованная версия',
  attempt: 'Попытка',
  limit: 'Лимит',
  source: 'Источник',
  materials: 'Материалы',
  login: 'Владелец',
  state: 'Вердикт владельца',
  revert_of: 'Revert выпуска',
}

const CHANGE_CLASS_LABELS: Record<string, string> = { frontend_ui: 'интерфейс', backend_api: 'сервер' }
const OUTCOME_LABELS: Record<string, string> = { ok: 'выполнено', blocked: 'заблокировано', error: 'ошибка' }
const RESULT_LABELS: Record<string, string> = { ok: 'успешно', denied: 'отказ', error: 'ошибка' }
const OWNER_STATE_LABELS: Record<string, string> = {
  requested: 'ждёт отзыва',
  approved: 'одобрено',
  changes_requested: 'запрошены изменения',
}
const CONCLUSION_LABELS: Record<string, string> = { success: 'успешно', failure: 'ошибка' }
const STEP_LABELS: Record<string, string> = {
  spec: 'требования',
  research: 'исследование',
  implement: 'реализация',
  verify: 'проверки разработчика',
  qa: 'тест QA',
  review: 'ревью критика',
  verify_branch: 'повторная проверка ветки',
  time: 'предел времени',
  graph: 'конвейер задачи',
  rework: 'доработка',
  git_worktree: 'рабочая копия',
  push: 'push ветки',
  git_push: 'push ветки',
  create_pr: 'создание PR',
  close_pr: 'закрытие PR',
  pr_checks: 'проверки PR',
  pr_review: 'ревью PR',
  owner_summary: 'сводка для владельца',
  owner_review: 'отзыв владельца',
  merge_pr: 'слияние PR',
  merge_checks: 'проверки итогового commit',
  release: 'выпуск',
  deploy: 'деплой',
  rollback: 'откат',
  revert: 'revert',
}
const REWORK_SOURCE_LABELS: Record<string, string> = {
  verify_failed: 'проверка после деплоя не пройдена',
  rolled_back: 'выпуск откатан администратором',
  checks_failed: 'проверки итогового commit не пройдены',
}
const MATERIAL_LABELS: Record<string, string> = {
  reason: 'причина',
  deploy_events: 'события деплоя',
  render_logs: 'журналы Render',
  actions_log: 'журнал GitHub Actions',
  source_diff: 'diff исходного изменения',
}
const SHA_KEYS = new Set(['commit_sha', 'qa_sha', 'head_sha', 'merge_sha', 'target_sha', 'published_sha'])

function mapped(labels: Record<string, string>, value: unknown): string {
  const key = String(value)
  return labels[key] ?? key
}

function plain(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function detailValue(key: string, value: unknown): string {
  if (SHA_KEYS.has(key) && typeof value === 'string') return short(value)
  switch (key) {
    case 'from':
    case 'to':
      return statusLabel(value)
    case 'change_class':
      return mapped(CHANGE_CLASS_LABELS, value)
    case 'outcome':
      return mapped(OUTCOME_LABELS, value)
    case 'result':
      return mapped(RESULT_LABELS, value)
    case 'decision':
      return decisionLabel(value)
    case 'render_status':
      return renderLabel(value)
    case 'verdict':
      return mapped(REVIEW_VERDICT_LABELS, value)
    case 'state':
      return mapped(OWNER_STATE_LABELS, value)
    case 'verified':
      return value === true ? 'пройдена' : 'не пройдена'
    case 'fault':
      return value === true ? 'да' : 'нет'
    case 'duration_ms':
      return typeof value === 'number' ? `${(value / 1000).toFixed(1)} с` : plain(value)
    case 'pr_number':
      return `#${String(value)}`
    case 'step':
    case 'kind':
      return mapped(STEP_LABELS, value)
    case 'source':
      return mapped(REWORK_SOURCE_LABELS, value)
    case 'materials':
      return Array.isArray(value) ? value.map((item) => mapped(MATERIAL_LABELS, item)).join(', ') : plain(value)
    default:
      return plain(value)
  }
}

function checkLines(checks: PullRequestCheck[]): DetailLine[] {
  return checks.map((check) => ({
    label: `Проверка ${check.name}`,
    value: check.conclusion
      ? mapped(CONCLUSION_LABELS, check.conclusion)
      : mapped(CHECK_STATUS_LABELS, check.status).toLowerCase(),
    ...(check.url ? { href: check.url } : {}),
  }))
}

function blank(value: unknown): boolean {
  return value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0)
}

export function describeEventDetails(event: TaskEvent): DetailLine[] {
  const known = event.type in EVENT_TYPE_LABELS
  const lines: DetailLine[] = []
  if (known && event.tool) lines.push({ label: 'Инструмент', value: event.tool })
  for (const [key, value] of Object.entries(event.payload)) {
    if (blank(value)) continue
    if (!known) {
      lines.push({ label: key, value: plain(value) })
      continue
    }
    if (key === 'checks' && Array.isArray(value)) {
      lines.push(...checkLines(value as PullRequestCheck[]))
      continue
    }
    const label = key === 'summary' && event.role === null ? 'Пояснение' : (DETAIL_LABELS[key] ?? key)
    const line: DetailLine = { label, value: detailValue(key, value) }
    if (key === 'pr_url' && typeof value === 'string') line.href = value
    lines.push(line)
  }
  if (known && event.artifact) {
    lines.push({
      label: 'Артефакт',
      value: event.artifact,
      href: `/api/tasks/${encodeURIComponent(event.task_id)}/artifacts/${encodeURIComponent(event.artifact)}`,
    })
  }
  return lines
}

function agentView(key: AgentKey, state: AgentState, extra: Partial<AgentView> = {}): AgentView {
  return {
    key,
    label: AGENT_LABELS[key],
    state,
    stateLabel: AGENT_STATE_LABELS[state],
    model: null,
    activity: null,
    note: null,
    ...extra,
  }
}

function lastModel(events: Events, role: AgentRole, from: number, until: number): ModelInfo | null {
  const index = lastIndex(events, (event) => event.tool === 'model' && event.role === role, from, until)
  return index < 0 ? null : modelOf(events[index])
}

function activityOf(events: Events, role: AgentRole, start: number, until: number): Phrase {
  const index = lastIndex(
    events,
    (event) =>
      (event.role === role && (event.type === 'tool_called' || event.type === 'check_result')) ||
      ((role === 'qa' || role === 'developer') && event.role === null && event.type === 'check_result'),
    start + 1,
    until,
  )
  const event = at(events, index)
  if (!event) return { text: STEP_ACTIVITY[role] }
  return event.type === 'check_result' ? checkPhrase(event) : toolPhrase(event, role)
}

interface Span {
  from: number
  until: number
  start: number
  finish: number
}

function settle(events: Events, role: AgentRole, span: Span): AgentView {
  const { from, until, start, finish } = span
  const model = lastModel(events, role, from, until)
  if (start < 0 && finish < 0) return agentView(role, 'waiting', { model })
  if (start > finish) {
    const stop = events.slice(start + 1, until).find(isStop)
    if (stop) return agentView(role, 'stopped', { model, note: stopNote(stop) })
    return agentView(role, 'working', { model, activity: activityOf(events, role, start, until) })
  }
  const event = events[finish]
  const outcome = event.payload.outcome
  const state: AgentState = event.type === 'pr_review' || outcome === 'ok' ? 'done' : outcome === 'blocked' ? 'blocked' : 'error'
  return agentView(role, state, { model, note: text(event.payload.summary) })
}

function roleAgent(events: Events, role: AgentRole, from: number, until = events.length): AgentView {
  return settle(events, role, {
    from,
    until,
    start: lastIndex(events, (event) => event.type === 'role_started' && event.role === role, from, until),
    finish: lastIndex(
      events,
      (event) => event.type === 'role_finished' && event.role === role && !isRevert(event),
      from,
      until,
    ),
  })
}

function reviewerAgent(events: Events, from: number, until: number): AgentView {
  const span = {
    from,
    until,
    start: lastIndex(events, (event) => event.tool === 'model' && event.role === 'pr_reviewer', from, until),
    finish: lastIndex(
      events,
      (event) =>
        !isRevert(event) &&
        (event.type === 'pr_review' || (event.type === 'role_finished' && event.role === 'pr_reviewer')),
      from,
      until,
    ),
  }
  const view = settle(events, 'pr_reviewer', span)
  const review = at(events, span.finish)
  if (view.state === 'done' && review?.type === 'pr_review') {
    return { ...view, note: review.payload.verdict === 'approve' ? 'PR принят' : 'PR отклонён' }
  }
  return view
}

function intakeAgent(events: Events): AgentView {
  const created = events.find((event) => event.type === 'task_created')
  const rejected = events.find((event) => event.type === 'task_rejected')
  const model = created ? modelOf(created) : null
  if (rejected) {
    return agentView('intake', 'error', {
      model,
      stateLabel: 'отклонил задачу',
      note: text(rejected.payload.reason),
    })
  }
  return agentView('intake', created ? 'done' : 'waiting', { model })
}

function stage(key: string, title: string, state: StageState, extra: Partial<Stage> = {}): Stage {
  return { key, title, state, detail: null, link: null, checks: [], results: [], ...extra }
}

function checksState(checks: PullRequestCheck[]): StageState {
  if (checks.some((check) => check.status === 'completed' && !passed(check))) return 'error'
  if (checks.length > 0 && checks.every((check) => check.status === 'completed')) return 'done'
  return 'running'
}

function resultLines(events: Events, sha: string, from: number): CheckLine[] {
  return events
    .slice(Math.max(from, 0))
    .filter(
      (event) =>
        event.type === 'check_result' && event.payload.commit_sha === sha && event.payload.qa_sha === sha,
    )
    .map((event) => ({
      name: text(event.payload.step) ?? text(event.payload.command) ?? 'проверка',
      ok: event.payload.exit_code === 0,
    }))
}

function repoUrl(prUrl: string | null): string | null {
  if (!prUrl || !/\/pull\/\d+$/.test(prUrl)) return null
  return prUrl.replace(/\/pull\/\d+$/, '')
}

function prLink(event: TaskEvent, revert: boolean): StageLink | null {
  const href = text(event.payload.pr_url)
  if (!href) return null
  return { href, label: `${revert ? 'revert-PR' : 'PR'} #${String(event.payload.pr_number)}` }
}

function unknownOf(events: Events, kinds: string[], from: number): TaskEvent | null {
  return at(
    events,
    lastIndex(
      events,
      (event) => event.type === 'operation_unknown' && !isRevert(event) && kinds.includes(String(event.payload.kind)),
      from,
    ),
  )
}

const GITHUB_TYPES = new Set(['pr_opened', 'checks_status', 'pr_review', 'merge_started', 'pr_merged'])

const resumed: Match = (event) => event.type === 'decision_recorded' && event.payload.decision === 'retry'

function haltAfter(events: Events, from: number, progress: Match): Stage | null {
  const halt = lastIndex(
    events,
    (event) =>
      event.type === 'role_finished' &&
      event.role === null &&
      !isRevert(event) &&
      !('release_id' in event.payload) &&
      event.payload.outcome !== 'ok',
    from,
  )
  if (halt < 0 || lastIndex(events, progress, halt + 1) >= 0) return null
  return stage('halt', 'Остановка', 'error', { detail: { text: text(events[halt].payload.summary) ?? 'нет данных' } })
}

function githubStages(events: Events, from: number, task: Task, reviewer: AgentView): { stages: Stage[]; mergeIndex: number } {
  const own = (type: string, extra: Match = () => true) => (event: TaskEvent) =>
    event.type === type && !isRevert(event) && extra(event)
  const pushIndex = lastIndex(events, (event) => own('tool_called')(event) && event.tool === 'git_push', from)
  const pushFail = lastIndex(events, own('role_finished', (event) => event.payload.step === 'git_push'), from)
  const opened = at(events, lastIndex(events, own('pr_opened'), from))
  const closedIndex = lastIndex(events, own('pr_closed'), from)
  const mergeIndex = lastIndex(events, own('pr_merged'), from)
  const merged = at(events, mergeIndex)
  const mergeSha = merged ? String(merged.payload.merge_sha ?? '') : null
  const checks = at(events, lastIndex(events, own('checks_status', (event) => event.payload.head_sha !== mergeSha), from))
  const reviewIndex = lastIndex(events, own('pr_review'), from)
  const review = at(events, reviewIndex)
  const reviewFail = lastIndex(
    events,
    own('role_finished', (event) => event.role === 'pr_reviewer' && event.payload.outcome !== 'ok'),
    from,
  )
  const mergeStarted = lastIndex(events, own('merge_started'), from)

  let push: Stage
  const pushEvent = at(events, pushIndex)
  const pushUnknown = unknownOf(events, ['push'], from)
  if (pushFail > pushIndex) {
    push = stage('push', 'Ветка и push', 'error', { detail: { text: text(events[pushFail].payload.summary) ?? 'нет данных' } })
  } else if (pushEvent) {
    const [branch, sha] = (text(pushEvent.payload.arguments) ?? '').split(' ')
    const failed = pushEvent.payload.result !== 'ok'
    push = stage('push', 'Ветка и push', failed ? 'error' : 'done', {
      detail: { text: 'ветка ', code: branch, tail: sha ? `, commit ${short(sha)}` : undefined },
    })
  } else if (pushUnknown) {
    push = stage('push', 'Ветка и push', 'error', { detail: { text: text(pushUnknown.payload.reason) ?? 'исход неизвестен' } })
  } else {
    push = stage('push', 'Ветка и push', 'waiting')
  }

  let pr: Stage
  const prUnknown = unknownOf(events, ['create_pr', 'close_pr'], from)
  if (opened && closedIndex > events.indexOf(opened)) {
    pr = stage('pr', 'Pull request', 'error', { detail: { text: 'PR закрыт' }, link: prLink(opened, false) })
  } else if (opened) {
    pr = stage('pr', 'Pull request', 'done', { link: prLink(opened, false) })
  } else if (prUnknown) {
    pr = stage('pr', 'Pull request', 'error', { detail: { text: text(prUnknown.payload.reason) ?? 'исход неизвестен' } })
  } else {
    pr = stage('pr', 'Pull request', 'waiting')
  }

  const list = checksOf(checks)
  const required = checks
    ? stage('checks', 'Обязательные проверки PR', checksState(list), {
        detail: { text: `${checksSummary(list)} для `, code: short(String(checks.payload.head_sha ?? '')) },
        checks: list,
      })
    : stage('checks', 'Обязательные проверки PR', 'waiting')

  let verdict: Stage
  if (reviewFail > reviewIndex) {
    verdict = stage('review', 'Вердикт агента ревью', 'error', {
      detail: { text: text(events[reviewFail].payload.summary) ?? 'нет данных' },
    })
  } else if (review) {
    const approve = review.payload.verdict === 'approve'
    const reasons = task.pr_review && task.pr_review.head_sha === review.payload.head_sha ? task.pr_review.reasons : null
    verdict = stage('review', 'Вердикт агента ревью', approve ? 'done' : 'error', {
      detail: { text: `${approve ? 'принят' : 'отклонён'}${reasons ? `: ${reasons}` : ''}` },
    })
  } else {
    verdict = stage('review', 'Вердикт агента ревью', reviewer.state === 'working' ? 'running' : 'waiting')
  }

  let merge: Stage
  const mergeUnknown = unknownOf(events, ['merge_pr'], from)
  if (merged && mergeSha) {
    const repo = repoUrl(text(opened?.payload.pr_url) ?? task.pr_url)
    merge = stage('merge', 'Слияние в main', 'done', {
      detail: { text: 'слит в main, commit ', code: short(mergeSha) },
      link: repo ? { href: `${repo}/commit/${mergeSha}`, label: `Итоговый commit ${short(mergeSha)}` } : null,
    })
  } else if (mergeUnknown) {
    merge = stage('merge', 'Слияние в main', 'error', { detail: { text: text(mergeUnknown.payload.reason) ?? 'исход неизвестен' } })
  } else if (mergeStarted >= 0) {
    merge = stage('merge', 'Слияние в main', 'running')
  } else {
    merge = stage('merge', 'Слияние в main', 'waiting')
  }

  const stages = [push, pr, required, verdict, merge]
  const integration = lastIndex(
    events,
    (event) => event.type === 'status_changed' && event.payload.to === 'awaiting_integration',
    from,
  )
  if (mergeIndex < 0 && integration >= 0) {
    const halt = haltAfter(
      events,
      integration + 1,
      (event) => resumed(event) || (!isRevert(event) && (GITHUB_TYPES.has(event.type) || event.tool === 'git_push')),
    )
    if (halt) stages.push(halt)
  }
  return { stages, mergeIndex }
}

const RELEASE_TYPES = new Set([
  'release_candidate',
  'deploy_status',
  'rollback_status',
  'release_blocked',
  'release_unblocked',
  'external_publish',
])

function candidateOf(releases: readonly ReleaseCandidate[], task: Task, sha: string | null): ReleaseCandidate | null {
  if (!sha) return null
  const matched = releases.filter((item) => item.task_id === task.id && item.kind === 'release' && item.commit_sha === sha)
  return matched.reduce<ReleaseCandidate | null>(
    (latest, item) => (latest === null || item.created_at > latest.created_at ? item : latest),
    null,
  )
}

function queueStage(candidate: ReleaseCandidate | null, final: TaskEvent | null, queued: boolean): Stage {
  const title = 'Очередь выпуска'
  if (final) {
    const ok = final.payload.outcome === 'ok'
    const label = candidate ? candidateLabel(candidate) : ok ? 'выпуск завершён' : 'выпуск остановлен'
    return stage('queue', title, ok ? 'done' : 'error', { detail: { text: label } })
  }
  if (candidate) {
    const label = candidateLabel(candidate)
    const state: StageState =
      candidate.status === 'pending'
        ? candidate.blocked_reason
          ? 'error'
          : 'waiting'
        : ['failed', 'rolled_back', 'revoked'].includes(candidate.status)
          ? 'error'
          : 'done'
    return stage('queue', title, state, { detail: { text: label } })
  }
  return queued ? stage('queue', title, 'running', { detail: { text: 'кандидат выпуска создан' } }) : stage('queue', title, 'waiting')
}

function candidateLabel(candidate: ReleaseCandidate): string {
  if (candidate.status === 'pending') {
    return candidate.blocked_reason ? `выпуск заблокирован: ${candidate.blocked_reason}` : 'ожидает выпуска'
  }
  if (candidate.status === 'superseded') {
    return candidate.superseded_by ? `в составе выпуска ${candidate.superseded_by}` : RELEASE_STATUS_LABELS.superseded
  }
  return RELEASE_STATUS_LABELS[candidate.status]
}

function revertRelease(events: Events, from: number, sha: string, releases: readonly ReleaseCandidate[], task: Task): string | null {
  const candidate = candidateOf(releases, task, sha)
  if (candidate) return candidateLabel(candidate)
  const created = at(
    events,
    lastIndex(events, (event) => event.type === 'release_candidate' && event.payload.commit_sha === sha, from),
  )
  const id = text(created?.payload.release_id)
  if (!id) return null
  const deploy = at(events, lastIndex(events, (event) => event.type === 'deploy_status' && event.payload.release_id === id, from))
  return deploy ? `деплой ${renderLabel(deploy.payload.render_status)}` : 'ожидает выпуска'
}

function revertStage(
  events: Events,
  from: number,
  repo: string | null,
  releases: readonly ReleaseCandidate[],
  task: Task,
): Stage {
  const title = 'Revert-PR'
  const own = (type: string) => (event: TaskEvent) => event.type === type && isRevert(event)
  const started = lastIndex(events, (event) => isRevert(event) && (event.tool === 'git_revert' || event.type === 'pr_opened'), from)
  if (started < 0) return stage('revert', title, 'waiting')
  const opened = at(events, lastIndex(events, own('pr_opened'), from))
  const link = opened ? prLink(opened, true) : null
  const merged = at(events, lastIndex(events, own('pr_merged'), from))
  const closed = lastIndex(events, own('pr_closed'), from)
  const halt = lastIndex(events, (event) => own('role_finished')(event) && event.payload.outcome !== 'ok', from)
  const progress = lastIndex(events, (event) => isRevert(event) && event.type !== 'role_finished', from)
  if (halt > progress) {
    return stage('revert', title, 'error', { link, detail: { text: text(events[halt].payload.summary) ?? 'нет данных' } })
  }
  if (merged) {
    const sha = String(merged.payload.merge_sha ?? '')
    const checks = checksOf(at(events, lastIndex(events, (event) => own('checks_status')(event) && event.payload.head_sha === sha, from)))
    const results = resultLines(events, sha, from)
    const failed = checksState(checks) === 'error' || results.some((line) => !line.ok)
    const release = revertRelease(events, from, sha, releases, task)
    return stage('revert', title, failed ? 'error' : 'done', {
      link: link ?? (repo ? { href: `${repo}/commit/${sha}`, label: `Revert-commit ${short(sha)}` } : null),
      detail: { text: 'слит в main, commit ', code: short(sha), tail: release ? `; выпуск revert-commit: ${release}` : undefined },
      checks,
      results,
    })
  }
  if (closed >= 0 && (!opened || closed > events.indexOf(opened))) {
    return stage('revert', title, 'error', { link, detail: { text: 'revert-PR закрыт' } })
  }
  const checks = checksOf(at(events, lastIndex(events, own('checks_status'), from)))
  return stage('revert', title, 'running', { link, checks })
}

function releaseStages(
  events: Events,
  from: number,
  task: Task,
  releases: readonly ReleaseCandidate[],
  mergeIndex: number,
): { stages: Stage[]; started: boolean } {
  const merged = at(events, mergeIndex)
  const mergeSha = merged ? String(merged.payload.merge_sha ?? '') : null
  const candidate = candidateOf(releases, task, mergeSha)
  const candidateIndex = mergeSha
    ? lastIndex(events, (event) => event.type === 'release_candidate' && event.payload.commit_sha === mergeSha, from)
    : -1
  const releaseId = candidate?.id ?? text(at(events, candidateIndex)?.payload.release_id)
  const own = (type: string, extra: Match = () => true) => (event: TaskEvent) =>
    event.type === type && !isRevert(event) && releaseId !== null && event.payload.release_id === releaseId && extra(event)
  const final = at(events, lastIndex(events, own('role_finished', (event) => event.payload.step === 'release'), from))
  const queued = candidateIndex >= 0
  const deploy = at(events, lastIndex(events, own('deploy_status'), from))
  const verified = at(events, lastIndex(events, own('deploy_status', (event) => 'verified' in event.payload), from))
  const rollback = at(events, lastIndex(events, own('rollback_status'), from))
  const block = at(
    events,
    lastIndex(events, (event) => own('release_blocked')(event) || own('release_unblocked')(event), from),
  )
  const external = at(events, lastIndex(events, (event) => event.type === 'external_publish', from))
  const revert = revertStage(events, from, repoUrl(task.pr_url), releases, task)
  const started =
    mergeIndex >= 0 ||
    candidate !== null ||
    revert.state !== 'waiting' ||
    lastIndex(events, (event) => RELEASE_TYPES.has(event.type), from) >= 0

  const halt =
    mergeIndex >= 0
      ? haltAfter(events, mergeIndex + 1, (event) => resumed(event) || (RELEASE_TYPES.has(event.type) && !isRevert(event)))
      : null
  let checks: Stage
  const checksTitle = 'Проверки итогового commit'
  if (mergeSha) {
    const status = at(
      events,
      lastIndex(events, (event) => event.type === 'checks_status' && !isRevert(event) && event.payload.head_sha === mergeSha, from),
    )
    const list = checksOf(status)
    const attempt = Math.max(mergeIndex, lastIndex(events, resumed, mergeIndex + 1)) + 1
    const results = resultLines(events, mergeSha, attempt)
    const stopped =
      lastIndex(
        events,
        (event) =>
          event.type === 'role_finished' &&
          event.role === null &&
          !isRevert(event) &&
          event.payload.step === 'merge_checks' &&
          event.payload.outcome !== 'ok',
        attempt,
      ) >= 0
    const confirmed = queued || candidate !== null
    const failed =
      (status !== null && checksState(list) === 'error') ||
      results.some((line) => !line.ok) ||
      stopped ||
      (halt !== null && !confirmed)
    const state: StageState = failed ? 'error' : confirmed ? 'done' : 'running'
    checks = stage('final_checks', checksTitle, state, {
      detail: { text: 'commit ', code: short(mergeSha) },
      checks: list,
      results,
    })
  } else {
    checks = stage('final_checks', checksTitle, 'waiting')
  }

  const queue = queueStage(candidate, final, queued)
  const joint = final?.payload.outcome === 'ok' && !deploy
  const deployTitle = 'Деплой Render'
  let deployStage: Stage
  if (deploy) {
    const status = String(deploy.payload.render_status)
    const state: StageState = status === 'live' ? 'done' : FAILED_RENDER.has(status) ? 'error' : 'running'
    deployStage = stage('deploy', deployTitle, state, { detail: { text: renderLabel(status) } })
  } else if (joint) {
    deployStage = stage('deploy', deployTitle, 'done', { detail: { text: 'выполнен в составе общего выпуска' } })
  } else if (final) {
    deployStage = stage('deploy', deployTitle, 'error', { detail: { text: text(final.payload.summary) ?? 'нет данных' } })
  } else if (candidate?.status === 'deploying') {
    deployStage = stage('deploy', deployTitle, 'running')
  } else {
    deployStage = stage('deploy', deployTitle, 'waiting')
  }

  const verifyTitle = 'Проверка после деплоя'
  let verify: Stage
  if (verified) {
    if (verified.payload.fault === true) {
      verify = stage('verify', verifyTitle, 'error', { detail: { text: 'управляемый сбой' } })
    } else {
      const ok = verified.payload.verified === true
      verify = stage('verify', verifyTitle, ok ? 'done' : 'error', { detail: { text: ok ? 'пройдена' : 'не пройдена' } })
    }
  } else if (joint) {
    verify = stage('verify', verifyTitle, 'done', { detail: { text: 'выполнена в составе общего выпуска' } })
  } else if (candidate?.status === 'verifying') {
    verify = stage('verify', verifyTitle, 'running')
  } else {
    verify = stage('verify', verifyTitle, 'waiting')
  }

  const rollbackTitle = 'Откат'
  const parts: string[] = []
  let rollbackState: StageState = 'waiting'
  let rollbackCode: string | undefined
  if (rollback) {
    const status = String(rollback.payload.render_status)
    parts.push(`Render: ${renderLabel(status)}`)
    if ('verified' in rollback.payload) {
      parts.push(rollback.payload.verified === true ? 'проверка пройдена' : 'проверка не пройдена')
      rollbackState = rollback.payload.verified === true ? 'done' : 'error'
    } else {
      rollbackState = status === 'live' ? 'done' : FAILED_RENDER.has(status) ? 'error' : 'running'
    }
    rollbackCode = text(rollback.payload.target_sha) ? short(String(rollback.payload.target_sha)) : undefined
  }
  if (block?.type === 'release_blocked') {
    parts.push(`выпуск заблокирован: ${text(block.payload.reason) ?? 'нет данных'}`)
    rollbackState = 'error'
  } else if (block?.type === 'release_unblocked') {
    parts.push('блокировка снята')
    if (rollbackState === 'waiting' || rollbackState === 'error') rollbackState = 'done'
  }
  const rollbackStage = stage('rollback', rollbackTitle, rollbackState, {
    detail: parts.length
      ? rollbackCode
        ? { text: `${parts.join('; ')}; версия `, code: rollbackCode }
        : { text: parts.join('; ') }
      : null,
  })

  const stages = [checks, queue, deployStage, verify, rollbackStage, revert]
  if (halt) stages.push(halt)
  if (final) {
    stages.push(
      stage('result', 'Итог', final.payload.outcome === 'ok' ? 'done' : 'error', {
        detail: { text: text(final.payload.summary) ?? 'нет данных' },
      }),
    )
  } else if (external) {
    stages.push(
      stage('result', 'Итог', 'waiting', {
        detail: { text: 'опубликована версия вне офиса ', code: short(String(external.payload.published_sha ?? '')) },
      }),
    )
  }
  return { stages, started }
}

function handoffs(agents: Record<AgentKey, AgentView>, researched: boolean, fixing: boolean): Handoff[] {
  const finished = (key: AgentKey) => FINISHED.has(agents[key].state)
  const working = (key: AgentKey) => agents[key].state === 'working'
  const source: AgentKey = researched ? 'researcher' : 'supervisor'
  const list: Handoff[] = [
    { key: 'task', from: ['intake'], to: ['supervisor'], label: 'задача', active: finished('intake') && working('supervisor') },
  ]
  if (researched) {
    list.push({
      key: 'questions',
      from: ['supervisor'],
      to: ['researcher'],
      label: 'вопросы для исследования',
      active: finished('supervisor') && working('researcher'),
    })
  }
  list.push({
    key: 'requirements',
    from: [source],
    to: ['developer'],
    label: 'требования',
    active: !fixing && finished(source) && working('developer'),
  })
  if (fixing) {
    list.push({
      key: 'findings',
      from: ['qa', 'critic'],
      to: ['developer'],
      label: 'замечания',
      active: working('developer'),
    })
  }
  list.push(
    {
      key: 'code',
      from: ['developer'],
      to: ['qa', 'critic'],
      label: 'код',
      active: finished('developer') && (working('qa') || working('critic')),
    },
    {
      key: 'reports',
      from: ['qa', 'critic'],
      to: ['pr_reviewer'],
      label: 'отчёты',
      active: finished('qa') && finished('critic') && working('pr_reviewer'),
    },
  )
  return list
}

export function handoffText(handoff: Handoff): string {
  const from = handoff.from.map((key) => AGENT_LABELS[key]).join(' и ')
  const to = handoff.to.map((key) => AGENT_LABELS[key]).join(' и ')
  const verb = handoff.from.length > 1 ? 'передают' : 'передаёт'
  return `${from} ${verb} ${handoff.label} → ${to}${handoff.active ? ', сейчас' : ''}`
}

export function buildPipeline(events: Events, task: Task, releases: readonly ReleaseCandidate[] = []): Pipeline {
  const reworkIndex = lastIndex(events, (event) => event.type === 'rework_started')
  const from = reworkIndex + 1
  const supervisor = roleAgent(events, 'supervisor', 0)
  const researcherView = roleAgent(events, 'researcher', 0)
  const researched = lastIndex(events, (event) => event.type === 'role_started' && event.role === 'researcher') >= 0
  const developerEver = lastIndex(events, (event) => event.role === 'developer') >= 0
  const researcher =
    !researched && researcherView.state === 'waiting' && supervisor.state === 'done' && (task.spec !== null || developerEver)
      ? agentView('researcher', 'skipped')
      : researcherView

  let developer = roleAgent(events, 'developer', from)
  const devStart = lastIndex(events, (event) => event.type === 'role_started' && event.role === 'developer', from)
  const fixing =
    devStart >= 0 &&
    lastIndex(
      events,
      (event) => event.type === 'role_finished' && (event.role === 'qa' || event.role === 'critic'),
      from,
      devStart,
    ) >= 0
  if (fixing) {
    const cycle = task.fix_cycles > 0 ? `цикл исправления ${task.fix_cycles} из 2` : 'цикл исправления'
    developer = { ...developer, note: developer.note ? `${cycle}. ${developer.note}` : cycle }
  }

  const ownMerge = lastIndex(events, (event) => event.type === 'pr_merged' && !isRevert(event), from)
  const reviewer = reviewerAgent(events, from, ownMerge >= 0 ? ownMerge : events.length)
  const agents: Record<AgentKey, AgentView> = {
    intake: intakeAgent(events),
    supervisor,
    researcher,
    developer,
    qa: roleAgent(events, 'qa', from),
    critic: roleAgent(events, 'critic', from),
    pr_reviewer: reviewer,
  }

  const github = githubStages(events, from, task, reviewer)
  const release = releaseStages(events, from, task, releases, github.mergeIndex)
  const reworkEvent = at(events, reworkIndex)
  const attempt = Number(reworkEvent?.payload.attempt ?? task.rework_count)
  const limit = Number(reworkEvent?.payload.limit ?? task.rework_limit)
  return {
    agents,
    handoffs: handoffs(agents, researched, fixing),
    github: github.stages,
    release: release.stages,
    releaseStarted: release.started,
    rework: attempt > 0 ? { attempt, limit } : null,
  }
}
