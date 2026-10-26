import { describe, expect, it } from 'vitest'
import type { AgentRole, ReleaseCandidate, Task, TaskEvent } from '@/api/types'
import {
  buildPipeline,
  describeEvent,
  describeEventDetails,
  handoffText,
  type Pipeline,
  type Stage,
} from '@/lib/pipeline'
import { releases, tasks } from '@/mocks/data'

const developing = tasks.find((task) => task.id === 'tsk_101') as Task
const merged = tasks.find((task) => task.id === 'tsk_102') as Task
const pending = releases.find((release) => release.status === 'pending') as ReleaseCandidate

const HEAD = 'b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f80'
const MERGE = merged.merge_sha as string
const REVERT_HEAD = 'd'.repeat(40)
const REVERT_MERGE = 'e'.repeat(40)

class Log {
  events: TaskEvent[] = []

  add(type: string, payload: Record<string, unknown> = {}, extra: Partial<TaskEvent> = {}): this {
    const seq = this.events.length + 1
    this.events.push({
      event_id: 5000 + seq,
      task_id: 'tsk_101',
      seq,
      type,
      timestamp: '2026-10-25T10:00:00Z',
      role: null,
      tool: null,
      artifact: null,
      payload,
      ...extra,
    })
    return this
  }

  status(from: string, to: string): this {
    return this.add('status_changed', { from, to })
  }

  started(role: AgentRole, step: string): this {
    return this.add('role_started', { step }, { role })
  }

  finished(role: AgentRole | null, step: string, outcome = 'ok', summary = 'Готово.'): this {
    return this.add('role_finished', { step, outcome, summary }, { role })
  }

  model(role: AgentRole, step: string, target: string, effort: string | null = 'medium'): this {
    const [provider, model] = target.split('/')
    const fields = effort ? { provider, model, effort } : {}
    return this.add('tool_called', { arguments: `${role}/${step} ${target}`, result: 'ok', ...fields }, { role, tool: 'model' })
  }

  tool(role: AgentRole | null, tool: string, args: string, result = 'ok', extra: Record<string, unknown> = {}): this {
    return this.add('tool_called', { arguments: args, result, ...extra }, { role, tool })
  }

  check(role: AgentRole | null, step: string, exitCode: number, context: Record<string, unknown> = {}): this {
    return this.add(
      'check_result',
      { command: `npm run ${step}`, exit_code: exitCode, duration_ms: 1000, step, ...context },
      { role, tool: 'sandbox' },
    )
  }

  intake(): this {
    return this.add('task_created', {
      title: 'Кнопка копирования',
      provider: 'anthropic',
      model: 'claude-haiku-5-5',
      effort: 'low',
    })
  }

  spec(research: boolean): this {
    this.intake().status('queued', 'awaiting_spec_approval').started('supervisor', 'spec')
    this.model('supervisor', 'spec', 'anthropic/claude-sonnet-5-5').finished('supervisor', 'spec')
    if (research) {
      this.started('researcher', 'research').model('researcher', 'research_plan', 'anthropic/claude-haiku-5-5', 'low')
      this.tool('researcher', 'fetch_public', 'https://developer.mozilla.org/ru/docs/Web/API/Clipboard')
      this.finished('researcher', 'research', 'ok', 'Источников: 1.')
    }
    return this.add('decision_required', { decision: 'approve_spec', expected_version: 3 })
      .add('decision_recorded', { decision: 'approve_spec' })
      .status('awaiting_spec_approval', 'developing')
  }

  develop(): this {
    return this.started('developer', 'implement')
      .model('developer', 'implement', 'anthropic/claude-sonnet-5-5', 'high')
      .tool('developer', 'read_file', 'apps/chat/web/src/components/chat/ChatThread.tsx')
  }

  review(): this {
    return this.tool('developer', 'git_commit', 'abc1234 (2 файлов)')
      .finished('developer', 'implement', 'ok', 'Добавлена кнопка.')
      .check(null, 'lint', 0, { commit_sha: HEAD, qa_sha: HEAD })
      .status('developing', 'reviewing')
      .started('qa', 'qa')
      .started('critic', 'review')
  }

  integrate(): this {
    return this.status('reviewing', 'awaiting_integration')
      .tool(null, 'git_push', `office/tsk_101 ${HEAD}`)
      .add('pr_opened', { pr_number: 12, pr_url: 'https://github.com/SlavaPush/vibe-studio/pull/12' })
      .add('checks_status', {
        head_sha: HEAD,
        checks: [
          { name: 'chat-web', status: 'completed', conclusion: 'success', url: 'https://github.com/run/1' },
          { name: 'office-paths', status: 'in_progress', conclusion: null, url: null },
        ],
      })
  }
}

function stageOf(stages: Stage[], key: string): Stage {
  return stages.find((stage) => stage.key === key) as Stage
}

function handoff(pipeline: Pipeline, key: string) {
  return pipeline.handoffs.find((item) => item.key === key)
}

const build = (log: Log, task: Task = developing, list: ReleaseCandidate[] = []) => buildPipeline(log.events, task, list)

describe('buildPipeline: агенты', () => {
  it('полный проход без исследования: модели, состояния и текущая задача разработчика', () => {
    const log = new Log().spec(false).develop()
    const { agents, handoffs } = build(log)
    expect(agents.intake).toMatchObject({ state: 'done', model: { provider: 'anthropic', model: 'claude-haiku-5-5', effort: 'low' } })
    expect(agents.supervisor).toMatchObject({ state: 'done', model: { model: 'claude-sonnet-5-5', effort: 'medium' } })
    expect(agents.researcher.state).toBe('skipped')
    expect(agents.researcher.stateLabel).toBe('не понадобился')
    expect(agents.developer).toMatchObject({
      state: 'working',
      stateLabel: 'работает',
      model: { provider: 'anthropic', model: 'claude-sonnet-5-5', effort: 'high' },
      activity: { text: 'читает ', code: 'ChatThread.tsx', title: 'apps/chat/web/src/components/chat/ChatThread.tsx' },
    })
    for (const key of ['qa', 'critic', 'pr_reviewer'] as const) {
      expect(agents[key]).toMatchObject({ state: 'waiting', model: null })
    }
    expect(handoffs.map((item) => item.key)).toEqual(['task', 'requirements', 'code', 'reports'])
    const requirements = handoffs.find((item) => item.key === 'requirements')
    expect(requirements).toMatchObject({ from: ['supervisor'], active: true })
    expect(handoffText(requirements!)).toBe('Supervisor передаёт требования → Разработчик, сейчас')
  })

  it('сценарий с исследованием: вопросы исследователю, требования от исследователя', () => {
    const log = new Log().intake().status('queued', 'awaiting_spec_approval').started('supervisor', 'spec')
    log.finished('supervisor', 'spec').started('researcher', 'research')
    log.tool('researcher', 'fetch_public', 'https://developer.mozilla.org/ru/docs/Web/API/Clipboard')
    let pipeline = build(log, { ...developing, status: 'awaiting_spec_approval', spec: null })
    expect(pipeline.agents.researcher.activity).toMatchObject({ text: 'читает страницу ', code: 'developer.mozilla.org' })
    expect(handoff(pipeline, 'questions')).toMatchObject({ active: true, label: 'вопросы для исследования' })
    log.finished('researcher', 'research', 'ok', 'Источников: 1.').status('awaiting_spec_approval', 'developing').develop()
    pipeline = build(log)
    expect(pipeline.agents.researcher.state).toBe('done')
    expect(handoff(pipeline, 'questions')?.active).toBe(false)
    expect(handoff(pipeline, 'requirements')).toMatchObject({ from: ['researcher'], active: true })
  })

  it('пока Supervisor работает, исследователь ожидает', () => {
    const log = new Log().intake().started('supervisor', 'spec')
    const pipeline = build(log, { ...developing, status: 'awaiting_spec_approval', spec: null })
    expect(pipeline.agents.supervisor.state).toBe('working')
    expect(pipeline.agents.supervisor.model).toBeNull()
    expect(pipeline.agents.researcher.state).toBe('waiting')
    expect(handoff(pipeline, 'task')?.active).toBe(true)
  })

  it('QA: несколько обращений к модели и резерв — «пишет тест Playwright», проверка без роли — у QA', () => {
    const log = new Log().spec(false).develop().review()
    log.model('qa', 'qa_try', 'anthropic/claude-sonnet-5-5').model('qa', 'qa_try', 'anthropic/claude-sonnet-5-5')
    log.model('qa', 'qa_try', 'openai/gpt-6.1-sol')
    let pipeline = build(log)
    expect(pipeline.agents.qa).toMatchObject({
      state: 'working',
      activity: { text: 'пишет тест Playwright' },
      model: { provider: 'openai', model: 'gpt-6.1-sol', effort: 'medium' },
    })
    expect(pipeline.agents.critic.activity).toEqual({ text: 'сверяет изменения с требованиями' })
    expect(pipeline.agents.developer.state).toBe('done')
    expect(handoff(pipeline, 'code')?.active).toBe(true)
    log.check(null, 'lint', 0, { commit_sha: HEAD, qa_sha: 'f'.repeat(40) })
    log.model('critic', 'review', 'anthropic/claude-sonnet-5-5')
    pipeline = build(log)
    expect(pipeline.agents.qa.activity).toEqual({ text: 'проверка ', code: 'lint', tail: ': успешно' })
    expect(pipeline.agents.critic.activity).toEqual({ text: 'обращается к модели' })
  })

  it('старые события модели без новых полей: модель из arguments, без уровня усилий', () => {
    const log = new Log().add('task_created', { title: 'x' }).started('supervisor', 'spec')
    log.model('supervisor', 'spec', 'anthropic/claude-sonnet-5-5', null)
    const { agents } = build(log)
    expect(agents.supervisor.model).toEqual({ provider: 'anthropic', model: 'claude-sonnet-5-5', effort: null })
    expect(agents.intake).toMatchObject({ state: 'done', model: null })
  })

  it('остановка без роли во время QA и критика: оба остановлены до нового role_started', () => {
    const log = new Log().spec(false).develop().review()
    log.finished(null, 'time', 'blocked', 'Превышен предел времени выполнения задачи.').status('reviewing', 'needs_attention')
    let { agents } = build(log)
    expect(agents.qa).toMatchObject({ state: 'stopped', note: 'Превышен предел времени выполнения задачи.' })
    expect(agents.critic.state).toBe('stopped')
    log.add('decision_recorded', { decision: 'retry' }).status('needs_attention', 'queued')
    expect(build(log).agents.qa.state).toBe('stopped')
    log.status('queued', 'reviewing').started('qa', 'qa')
    ;({ agents } = build(log))
    expect(agents.qa.state).toBe('working')
    expect(agents.critic.state).toBe('stopped')
  })

  it('цикл исправления: замечания QA и критика возвращаются разработчику', () => {
    const log = new Log().spec(false).develop().review()
    log.finished('qa', 'qa', 'blocked', 'Тест не проходит.').finished('critic', 'review', 'ok', 'Замечаний нет.')
    log.status('reviewing', 'developing').started('developer', 'implement')
    const pipeline = build(log, { ...developing, fix_cycles: 1 })
    expect(pipeline.agents.developer).toMatchObject({ state: 'working', note: 'цикл исправления 1 из 2' })
    expect(pipeline.agents.qa).toMatchObject({ state: 'blocked', stateLabel: 'завершил с замечаниями' })
    expect(handoff(pipeline, 'findings')).toMatchObject({ active: true, from: ['qa', 'critic'] })
    expect(handoff(pipeline, 'requirements')?.active).toBe(false)
    expect(handoffText(handoff(pipeline, 'findings')!)).toBe('QA и Критик передают замечания → Разработчик, сейчас')
  })

  it('отчёты агенту ревью PR и его вердикт', () => {
    const log = new Log().spec(false).develop().review()
    log.finished('qa', 'qa').finished('critic', 'review').integrate()
    log.model('pr_reviewer', 'pr_review', 'anthropic/claude-sonnet-5-5', 'high')
    let pipeline = build(log)
    expect(pipeline.agents.pr_reviewer).toMatchObject({
      state: 'working',
      activity: { text: 'проверяет PR' },
      model: { model: 'claude-sonnet-5-5', effort: 'high' },
    })
    expect(handoff(pipeline, 'reports')?.active).toBe(true)
    expect(stageOf(pipeline.github, 'review').state).toBe('running')
    log.add('pr_review', { review_id: 'prv_1', head_sha: HEAD, verdict: 'reject' }, { role: 'pr_reviewer' })
    pipeline = build(log)
    expect(pipeline.agents.pr_reviewer).toMatchObject({ state: 'done', note: 'PR отклонён' })
  })

  it('ошибка роли и отклонение фильтром приёма', () => {
    const log = new Log().spec(false).develop().finished('developer', 'implement', 'error', 'Модель недоступна.')
    expect(build(log).agents.developer).toMatchObject({ state: 'error', note: 'Модель недоступна.' })
    const rejected = new Log().intake().add('task_rejected', { reason: 'Задача не относится к интерфейсу.' })
    expect(build(rejected).agents.intake).toMatchObject({
      state: 'error',
      stateLabel: 'отклонил задачу',
      note: 'Задача не относится к интерфейсу.',
    })
  })

  it('неизвестный тип события не ломает схему', () => {
    const log = new Log().spec(false).add('owner_review_requested', { pr_number: 12 })
    expect(() => build(log)).not.toThrow()
    expect(describeEvent(log.events[log.events.length - 1])).toEqual({ text: 'Событие ', code: 'owner_review_requested' })
  })

  it('доработка после отката: фильтр, Supervisor и требования сохраняются, остальное — заново', () => {
    const log = new Log().spec(false).develop().review().finished('qa', 'qa').finished('critic', 'review').integrate()
    log.add('pr_review', { review_id: 'prv_1', head_sha: HEAD, verdict: 'approve' }, { role: 'pr_reviewer' })
    log.add('pr_merged', { pr_number: 12, merge_sha: MERGE })
    log.add('rework_started', { attempt: 1, limit: 1, reason: 'Откат.', source: 'rolled_back', release_id: 'rel_7' })
    log.status('failed', 'developing').started('developer', 'implement')
    const pipeline = build(log, { ...developing, rework_count: 1, rework_limit: 1 })
    expect(pipeline.rework).toEqual({ attempt: 1, limit: 1 })
    expect(pipeline.agents.intake.state).toBe('done')
    expect(pipeline.agents.supervisor.state).toBe('done')
    expect(pipeline.agents.researcher.state).toBe('skipped')
    expect(pipeline.agents.developer.state).toBe('working')
    expect(pipeline.agents.qa.state).toBe('waiting')
    expect(pipeline.agents.pr_reviewer.state).toBe('waiting')
    expect(handoff(pipeline, 'requirements')?.active).toBe(true)
    expect(stageOf(pipeline.github, 'push').state).toBe('waiting')
    expect(stageOf(pipeline.github, 'merge').state).toBe('waiting')
    expect(pipeline.releaseStarted).toBe(false)
  })
})

describe('buildPipeline: GitHub', () => {
  const approved = () => {
    const log = new Log().spec(false).develop().review().finished('qa', 'qa').finished('critic', 'review').integrate()
    log.model('pr_reviewer', 'pr_review', 'anthropic/claude-sonnet-5-5', 'high')
    return log.add('pr_review', { review_id: 'prv_1', head_sha: HEAD, verdict: 'approve' }, { role: 'pr_reviewer' })
  }

  it('push, PR, проверки со ссылками, вердикт с обоснованием и слияние', () => {
    const log = approved()
    let { github } = build(log, merged)
    expect(stageOf(github, 'push')).toMatchObject({ state: 'done', detail: { code: 'office/tsk_101', tail: ', commit b7c8d9e' } })
    expect(stageOf(github, 'pr')).toMatchObject({
      state: 'done',
      link: { href: 'https://github.com/SlavaPush/vibe-studio/pull/12', label: 'PR #12' },
    })
    const checks = stageOf(github, 'checks')
    expect(checks.state).toBe('running')
    expect(checks.checks.map((check) => check.url)).toEqual(['https://github.com/run/1', null])
    expect(stageOf(github, 'review')).toMatchObject({
      state: 'done',
      detail: { text: `принят: ${merged.pr_review?.reasons}` },
    })
    expect(stageOf(github, 'merge').state).toBe('waiting')
    log.add('merge_started', { pr_number: 12, head_sha: HEAD })
    expect(stageOf(build(log, merged).github, 'merge').state).toBe('running')
    log.add('pr_merged', { pr_number: 12, merge_sha: MERGE })
    ;({ github } = build(log, merged))
    expect(stageOf(github, 'merge')).toMatchObject({
      state: 'done',
      detail: { code: MERGE.slice(0, 7) },
      link: { href: `https://github.com/SlavaPush/vibe-studio/commit/${MERGE}` },
    })
  })

  it('отказ push и закрытый PR показываются ошибкой', () => {
    const log = new Log().spec(false).status('reviewing', 'awaiting_integration')
    log.finished(null, 'git_push', 'blocked', 'GitHub отклонил push.')
    expect(stageOf(build(log).github, 'push')).toMatchObject({ state: 'error', detail: { text: 'GitHub отклонил push.' } })
    const closed = approved().add('pr_closed', { pr_number: 12 })
    expect(stageOf(build(closed).github, 'pr')).toMatchObject({ state: 'error', detail: { text: 'PR закрыт' } })
  })
})

describe('buildPipeline: выпуск', () => {
  const mergedLog = () => {
    const log = new Log().spec(false).develop().review()
    log.check(null, 'e2e', 0, { commit_sha: HEAD, qa_sha: 'f'.repeat(40) })
    log.finished('qa', 'qa').finished('critic', 'review').integrate()
    log.add('pr_review', { review_id: 'prv_1', head_sha: HEAD, verdict: 'approve' }, { role: 'pr_reviewer' })
    log.add('pr_merged', { pr_number: 12, merge_sha: MERGE })
    log.add('checks_status', {
      head_sha: MERGE,
      checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: 'https://github.com/run/9' }],
    })
    log.check(null, 'lint', 0, { commit_sha: MERGE, qa_sha: MERGE }).check(null, 'e2e', 0, { commit_sha: MERGE, qa_sha: MERGE })
    log.check(null, 'e2e', 1, { commit_sha: MERGE, qa_sha: 'f'.repeat(40) })
    return log.add('release_candidate', { release_id: 'rel_7', commit_sha: MERGE })
  }

  it('проверки итогового commit выполняются, пока набор не подтверждён кандидатом выпуска', () => {
    const log = new Log().spec(false).status('reviewing', 'awaiting_integration')
    log.add('pr_merged', { pr_number: 12, merge_sha: MERGE })
    log.add('checks_status', {
      head_sha: MERGE,
      checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: null }],
    })
    log.check(null, 'lint', 0, { commit_sha: MERGE, qa_sha: MERGE })
    expect(stageOf(build(log, merged).release, 'final_checks').state).toBe('running')
    log.finished(null, 'release', 'blocked', `Критический E2E итогового commit ${MERGE.slice(0, 7)} не выполнен: нет Docker.`)
    const { release } = build(log, merged)
    expect(stageOf(release, 'final_checks').state).toBe('error')
    expect(stageOf(release, 'halt').detail?.text).toContain('Критический E2E итогового commit')
  })

  it('после остановки merge_checks и повтора успешный набор показывается «готово»', () => {
    const log = new Log().spec(false).status('reviewing', 'awaiting_integration')
    log.add('pr_merged', { pr_number: 12, merge_sha: MERGE })
    log.add('checks_status', {
      head_sha: MERGE,
      checks: [{ name: 'chat-web', status: 'completed', conclusion: 'failure', url: null }],
    })
    log.check(null, 'e2e', 1, { commit_sha: MERGE, qa_sha: MERGE })
    log.finished(null, 'merge_checks', 'blocked', 'Родительский commit не получен.').status('awaiting_integration', 'needs_attention')
    expect(stageOf(build(log, merged).release, 'final_checks').state).toBe('error')
    log.add('decision_recorded', { decision: 'retry' }).status('needs_attention', 'awaiting_integration')
    log.add('checks_status', {
      head_sha: MERGE,
      checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: null }],
    })
    log.check(null, 'e2e', 0, { commit_sha: MERGE, qa_sha: MERGE })
    expect(stageOf(build(log, merged).release, 'final_checks').state).toBe('running')
    log.add('release_candidate', { release_id: 'rel_7', commit_sha: MERGE })
    const { release } = build(log, merged)
    expect(stageOf(release, 'final_checks')).toMatchObject({ state: 'done', results: [{ name: 'e2e', ok: true }] })
    expect(release.some((item) => item.key === 'halt')).toBe(false)
  })

  it.each(['neutral', 'skipped'])('заключение %s обязательной проверки — не успех', (conclusion) => {
    const log = new Log().integrate()
    log.add('checks_status', { head_sha: HEAD, checks: [{ name: 'chat-web', status: 'completed', conclusion, url: null }] })
    expect(stageOf(build(log).github, 'checks').state).toBe('error')
    expect(describeEvent(log.events[log.events.length - 1]).text).toBe('Обязательные проверки: 0 из 1 успешно для ')
  })

  it('события другого кандидата той же задачи не меняют этапы исходного выпуска', () => {
    const log = mergedLog()
    log.add('deploy_status', { release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'live' })
    log.add('deploy_status', { release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'live', verified: false, fault: false })
    log.add('role_finished', { step: 'release', outcome: 'error', summary: 'Проверка после деплоя не пройдена.', release_id: 'rel_7' })
    const revert = { revert_of: 'rel_7' }
    log.tool(null, 'git_revert', `office/revert-rel_7 ${MERGE.slice(0, 7)}`, 'ok', revert)
    log.add('pr_opened', { pr_number: 13, pr_url: 'https://github.com/SlavaPush/vibe-studio/pull/13', ...revert })
    log.add('pr_merged', { pr_number: 13, merge_sha: REVERT_MERGE, ...revert })
    log.add('release_candidate', { release_id: 'rel_8', commit_sha: REVERT_MERGE })
    log.add('deploy_status', { release_id: 'rel_8', render_deploy_id: 'dep_2', render_status: 'build_in_progress' })
    log.add('deploy_status', { release_id: 'rel_8', render_deploy_id: 'dep_2', render_status: 'live', verified: true, fault: false })
    log.add('role_finished', { step: 'release', outcome: 'ok', summary: 'Опубликовано: eeeeeee.', release_id: 'rel_8' })
    const failed = { ...pending, status: 'failed' as const }
    const revertRelease = { ...pending, id: 'rel_8', commit_sha: REVERT_MERGE, status: 'released' as const, origin: 'auto_revert' as const }
    for (const list of [[failed, revertRelease], []]) {
      const { release } = build(log, merged, list)
      expect(stageOf(release, 'deploy')).toMatchObject({ state: 'done', detail: { text: 'опубликован' } })
      expect(stageOf(release, 'verify')).toMatchObject({ state: 'error', detail: { text: 'не пройдена' } })
      expect(stageOf(release, 'queue').state).toBe('error')
      expect(stageOf(release, 'result')).toMatchObject({ state: 'error', detail: { text: 'Проверка после деплоя не пройдена.' } })
    }
    expect(stageOf(build(log, merged, [failed, revertRelease]).release, 'revert').detail?.tail).toBe(
      '; выпуск revert-commit: Опубликован',
    )
    expect(stageOf(build(log, merged).release, 'revert').detail?.tail).toBe('; выпуск revert-commit: деплой опубликован')
  })

  it('без событий выпуска блок ждёт слияния', () => {
    expect(build(new Log().spec(false).develop()).releaseStarted).toBe(false)
  })

  it('проверки итогового commit — только по merge_sha, PR-проверки — без итогового commit', () => {
    const { release, github, releaseStarted } = build(mergedLog(), merged, [pending])
    expect(releaseStarted).toBe(true)
    const checks = stageOf(release, 'final_checks')
    expect(checks.state).toBe('done')
    expect(checks.results).toEqual([
      { name: 'lint', ok: true },
      { name: 'e2e', ok: true },
    ])
    expect(checks.checks.map((check) => check.url)).toEqual(['https://github.com/run/9'])
    expect(stageOf(github, 'checks').detail?.code).toBe(HEAD.slice(0, 7))
  })

  it('очередь: ожидание, блокировка и включение в новый выпуск без номера места', () => {
    const log = mergedLog()
    expect(stageOf(build(log, merged, [pending]).release, 'queue')).toMatchObject({
      state: 'waiting',
      detail: { text: 'ожидает выпуска' },
    })
    const blocked = { ...pending, blocked_reason: 'выпуск rel_5 откатан' }
    expect(stageOf(build(log, merged, [blocked]).release, 'queue')).toMatchObject({
      state: 'error',
      detail: { text: 'выпуск заблокирован: выпуск rel_5 откатан' },
    })
    const older = { ...pending, status: 'superseded' as const, superseded_by: 'rel_9' }
    const other = { ...pending, id: 'rel_8', commit_sha: 'a'.repeat(40), created_at: '2026-10-26T00:00:00Z' }
    expect(stageOf(build(log, merged, [older, other]).release, 'queue')).toMatchObject({
      state: 'done',
      detail: { text: 'в составе выпуска rel_9' },
    })
  })

  it('совместный выпуск двух задач без собственного deploy_status', () => {
    const log = mergedLog().add('role_finished', {
      step: 'release',
      outcome: 'ok',
      summary: 'Опубликовано в составе выпуска rel_9.',
      release_id: 'rel_7',
    })
    const superseded = { ...pending, status: 'superseded' as const, superseded_by: 'rel_9' }
    const { release } = build(log, merged, [superseded])
    expect(stageOf(release, 'queue')).toMatchObject({ state: 'done', detail: { text: 'в составе выпуска rel_9' } })
    expect(stageOf(release, 'deploy')).toMatchObject({ state: 'done', detail: { text: 'выполнен в составе общего выпуска' } })
    expect(stageOf(release, 'verify').state).toBe('done')
    expect(stageOf(release, 'result')).toMatchObject({ state: 'done', detail: { text: 'Опубликовано в составе выпуска rel_9.' } })
  })

  it('отказ Render до первого deploy_status', () => {
    const log = mergedLog().add('role_finished', {
      step: 'release',
      outcome: 'error',
      summary: 'Render отклонил запрос деплоя: HTTP 500.',
      release_id: 'rel_7',
    })
    const { release } = build(log, merged, [{ ...pending, status: 'failed' }])
    expect(stageOf(release, 'queue').state).toBe('error')
    expect(stageOf(release, 'deploy')).toMatchObject({
      state: 'error',
      detail: { text: 'Render отклонил запрос деплоя: HTTP 500.' },
    })
  })

  it('статусы деплоя, проверка после деплоя и откат', () => {
    const log = mergedLog().add('deploy_status', { release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'build_in_progress' })
    let { release } = build(log, merged, [{ ...pending, status: 'deploying' }])
    expect(stageOf(release, 'queue').state).toBe('done')
    expect(stageOf(release, 'deploy')).toMatchObject({ state: 'running', detail: { text: 'сборка' } })
    log.add('deploy_status', { release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'live' })
    log.add('deploy_status', { release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'live', verified: false, fault: false })
    ;({ release } = build(log, merged))
    expect(stageOf(release, 'deploy')).toMatchObject({ state: 'done', detail: { text: 'опубликован' } })
    expect(stageOf(release, 'verify')).toMatchObject({ state: 'error', detail: { text: 'не пройдена' } })
    log.add('rollback_status', { release_id: 'rel_7', render_deploy_id: null, render_status: 'requested', target_sha: 'a'.repeat(40) })
    expect(stageOf(build(log, merged).release, 'rollback').state).toBe('running')
    log.add('rollback_status', { release_id: 'rel_7', render_deploy_id: 'dep_0', render_status: 'live', target_sha: 'a'.repeat(40), verified: true })
    log.add('release_blocked', { release_id: 'rel_7', blocked_by: 'rel_7', reason: 'Выпуск откатан.' })
    expect(stageOf(build(log, merged).release, 'rollback')).toMatchObject({
      state: 'error',
      detail: { text: 'Render: опубликован; проверка пройдена; выпуск заблокирован: Выпуск откатан.; версия ', code: 'aaaaaaa' },
    })
    log.add('release_unblocked', { release_id: 'rel_7', fixed_by: 'rel_8' })
    expect(stageOf(build(log, merged).release, 'rollback').state).toBe('done')
  })

  it('управляемый сбой проверки после деплоя', () => {
    const log = mergedLog().add('deploy_status', {
      release_id: 'rel_7', render_deploy_id: 'dep_1', render_status: 'live', verified: false, fault: true,
    })
    expect(stageOf(build(log, merged).release, 'verify')).toMatchObject({ state: 'error', detail: { text: 'управляемый сбой' } })
  })

  it('revert-PR по revert_of не попадает в GitHub, его проверки — по своему merge_sha', () => {
    const log = mergedLog()
    const revert = { revert_of: 'rel_7' }
    log.tool(null, 'git_revert', `office/revert-rel_7 ${MERGE.slice(0, 7)}`, 'ok', revert)
    log.add('pr_opened', { pr_number: 13, pr_url: 'https://github.com/SlavaPush/vibe-studio/pull/13', ...revert })
    log.add('checks_status', { head_sha: REVERT_HEAD, checks: [{ name: 'chat-web', status: 'in_progress', conclusion: null, url: null }], ...revert })
    log.add('pr_review', { review_id: 'prv_2', head_sha: REVERT_HEAD, verdict: 'approve', ...revert }, { role: 'pr_reviewer' })
    let pipeline = build(log, merged)
    expect(stageOf(pipeline.release, 'revert')).toMatchObject({
      state: 'running',
      link: { href: 'https://github.com/SlavaPush/vibe-studio/pull/13', label: 'revert-PR #13' },
    })
    log.add('pr_merged', { pr_number: 13, merge_sha: REVERT_MERGE, ...revert })
    log.add('checks_status', {
      head_sha: REVERT_MERGE,
      checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: 'https://github.com/run/13' }],
      ...revert,
    })
    log.check(null, 'e2e', 0, { commit_sha: REVERT_MERGE, qa_sha: REVERT_MERGE })
    pipeline = build(log, merged)
    const stage = stageOf(pipeline.release, 'revert')
    expect(stage.state).toBe('done')
    expect(stage.checks.map((check) => check.url)).toEqual(['https://github.com/run/13'])
    expect(stage.results).toEqual([{ name: 'e2e', ok: true }])
    expect(stageOf(pipeline.github, 'pr').link?.label).toBe('PR #12')
    expect(stageOf(pipeline.github, 'merge').detail?.code).toBe(MERGE.slice(0, 7))
    expect(stageOf(pipeline.github, 'checks').detail?.code).toBe(HEAD.slice(0, 7))
    expect(stageOf(pipeline.release, 'final_checks').results).toEqual([
      { name: 'lint', ok: true },
      { name: 'e2e', ok: true },
    ])
    expect(pipeline.agents.pr_reviewer).toMatchObject({ state: 'done', note: 'PR принят' })
  })
})

describe('describeEvent', () => {
  it('описывает события по-русски без вложенных объектов', () => {
    const log = new Log()
      .tool('developer', 'write_file', 'apps/chat/web/src/App.tsx (120 байт)')
      .tool('developer', 'write_file', 'apps/office/api/app/main.py (5 байт)', 'denied')
      .tool('qa', 'run_check', 'deploy', 'denied')
      .model('critic', 'review', 'openai/gpt-6.1-sol', 'high')
      .add('checks_status', { head_sha: HEAD, checks: [{ name: 'chat-web', status: 'completed', conclusion: 'success', url: null }] })
      .add('deploy_status', { release_id: 'rel_7', render_status: 'live' })
    expect(log.events.map(describeEvent)).toEqual([
      { text: 'изменяет ', code: 'App.tsx', title: 'apps/chat/web/src/App.tsx' },
      { text: 'изменяет ', code: 'main.py', title: 'apps/office/api/app/main.py', tail: ' — отказ' },
      { text: 'проверка запрещена политикой' },
      { text: 'обращается к модели ', code: 'openai/gpt-6.1-sol', tail: ', усилие high' },
      { text: 'Обязательные проверки: 1 из 1 успешно для ', code: HEAD.slice(0, 7) },
      { text: 'Деплой Render: опубликован' },
    ])
  })

  it('строка смены статуса показывает причину остановки', () => {
    const log = new Log()
      .status('developing', 'reviewing')
      .add('status_changed', { from: 'developing', to: 'needs_attention', reason: 'Разработчик не изменил файлы.' })
    expect(log.events.map(describeEvent)).toEqual([
      { text: 'Статус: Проверка' },
      { text: 'Статус: Требует внимания. Причина: Разработчик не изменил файлы.' },
    ])
  })
})

describe('describeEventDetails', () => {
  const details = (log: Log) => log.events.map(describeEventDetails)

  it('смена статуса с причиной', () => {
    const log = new Log().add('status_changed', { from: 'developing', to: 'needs_attention', reason: 'Нужен сервер.' })
    expect(details(log)).toEqual([
      [
        { label: 'Было', value: 'Разработка' },
        { label: 'Стало', value: 'Требует внимания' },
        { label: 'Причина', value: 'Нужен сервер.' },
      ],
    ])
  })

  it('итог роли словами агента и пояснение офиса', () => {
    const log = new Log()
      .finished('developer', 'implement', 'blocked', 'Выход требует серверного эндпоинта.')
      .finished(null, 'verify_branch', 'error', 'Повторная проверка ветки не пройдена.')
    expect(details(log)).toEqual([
      [
        { label: 'Шаг', value: 'реализация' },
        { label: 'Итог', value: 'заблокировано' },
        { label: 'Итог словами агента', value: 'Выход требует серверного эндпоинта.' },
      ],
      [
        { label: 'Шаг', value: 'повторная проверка ветки' },
        { label: 'Итог', value: 'ошибка' },
        { label: 'Пояснение', value: 'Повторная проверка ветки не пройдена.' },
      ],
    ])
  })

  it('обращение к модели: инструмент, аргументы, результат, провайдер, модель, усилие', () => {
    const log = new Log().model('critic', 'review', 'openai/gpt-6.1-sol', 'high')
    expect(details(log)).toEqual([
      [
        { label: 'Инструмент', value: 'model' },
        { label: 'Аргументы', value: 'critic/review openai/gpt-6.1-sol' },
        { label: 'Результат', value: 'успешно' },
        { label: 'Провайдер', value: 'openai' },
        { label: 'Модель', value: 'gpt-6.1-sol' },
        { label: 'Усилие', value: 'high' },
      ],
    ])
  })

  it('обязательные проверки: каждая проверка с заключением и head', () => {
    const url = 'https://github.com/SlavaPush/vibe-studio/actions/runs/1/job/2'
    const log = new Log().add('checks_status', {
      head_sha: HEAD,
      checks: [
        { name: 'chat-web', status: 'completed', conclusion: 'success', url },
        { name: 'chat-api', status: 'completed', conclusion: 'failure', url: null },
        { name: 'office-paths', status: 'in_progress', conclusion: null, url: null },
      ],
    })
    expect(details(log)).toEqual([
      [
        { label: 'Head', value: HEAD.slice(0, 7) },
        { label: 'Проверка chat-web', value: 'успешно', href: url },
        { label: 'Проверка chat-api', value: 'ошибка' },
        { label: 'Проверка office-paths', value: 'выполняется' },
      ],
    ])
  })

  it('слияние PR: номер и commit слияния; ссылка на артефакт', () => {
    const log = new Log()
      .add('pr_merged', { pr_number: 12, merge_sha: MERGE })
      .add('artifact_ready', { name: 'spec.json', size_bytes: 512 }, { artifact: 'spec.json' })
    expect(details(log)).toEqual([
      [
        { label: 'PR', value: '#12' },
        { label: 'Commit слияния', value: MERGE.slice(0, 7) },
      ],
      [
        { label: 'Имя', value: 'spec.json' },
        { label: 'Размер, байт', value: '512' },
        { label: 'Артефакт', value: 'spec.json', href: '/api/tasks/tsk_101/artifacts/spec.json' },
      ],
    ])
  })

  it('доработка и неизвестный исход операции: источник, материалы и операция по-русски', () => {
    const log = new Log()
      .add('rework_started', {
        reason: 'Проверка после деплоя не пройдена.',
        attempt: 1,
        limit: 1,
        source: 'verify_failed',
        release_id: 'rel_7',
        materials: ['reason', 'render_logs', 'new_source'],
      })
      .add('operation_unknown', { kind: 'merge_pr', pr_number: 15, reason: 'GitHub недоступен.' })
      .finished(null, 'future_step', 'error', 'x')
    expect(details(log)).toEqual([
      [
        { label: 'Причина', value: 'Проверка после деплоя не пройдена.' },
        { label: 'Попытка', value: '1' },
        { label: 'Лимит', value: '1' },
        { label: 'Источник', value: 'проверка после деплоя не пройдена' },
        { label: 'Выпуск', value: 'rel_7' },
        { label: 'Материалы', value: 'причина, журналы Render, new_source' },
      ],
      [
        { label: 'Операция', value: 'слияние PR' },
        { label: 'PR', value: '#15' },
        { label: 'Причина', value: 'GitHub недоступен.' },
      ],
      [
        { label: 'Шаг', value: 'future_step' },
        { label: 'Итог', value: 'ошибка' },
        { label: 'Пояснение', value: 'x' },
      ],
    ])
  })

  it('неизвестный тип: ключ и значение без пересказа, пустые значения пропускаются', () => {
    const log = new Log().add('future_event', { alpha: 'x', beta: 2, gamma: null, delta: '', nested: { a: 1 } })
    expect(details(log)).toEqual([
      [
        { label: 'alpha', value: 'x' },
        { label: 'beta', value: '2' },
        { label: 'nested', value: '{"a":1}' },
      ],
    ])
  })
})
