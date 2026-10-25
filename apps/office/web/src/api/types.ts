export const ROLES = ['participant', 'admin'] as const
export type Role = (typeof ROLES)[number]

export const TASK_STATUSES = [
  'rejected',
  'queued',
  'awaiting_spec_approval',
  'developing',
  'reviewing',
  'awaiting_integration',
  'awaiting_release',
  'deploying',
  'released',
  'failed',
  'cancelled',
  'needs_attention',
] as const
export type TaskStatus = (typeof TASK_STATUSES)[number]

export const SLOT_STATUSES: readonly TaskStatus[] = ['awaiting_spec_approval', 'developing', 'reviewing']

export const FAILURE_KINDS = ['checks_failed', 'pr_rejected', 'deploy_failed', 'verify_failed', 'rolled_back'] as const
export type FailureKind = (typeof FAILURE_KINDS)[number]

export const SLOTS_TOTAL = 3

export const MODEL_PROFILES = ['local', 'cloud', 'hybrid'] as const
export type ModelProfile = (typeof MODEL_PROFILES)[number]

export const AGENT_ROLES = ['supervisor', 'researcher', 'developer', 'qa', 'critic', 'pr_reviewer'] as const
export type AgentRole = (typeof AGENT_ROLES)[number]

export const TASK_EVENT_TYPES = [
  'task_created',
  'task_rejected',
  'status_changed',
  'role_started',
  'role_finished',
  'tool_called',
  'artifact_ready',
  'decision_required',
  'decision_recorded',
  'check_result',
  'release_candidate',
  'deploy_status',
  'pr_opened',
  'checks_status',
  'pr_closed',
  'operation_unknown',
  'pr_review',
  'merge_started',
  'pr_merged',
  'rollback_status',
  'release_blocked',
  'release_unblocked',
  'external_publish',
  'rework_started',
] as const
export type TaskEventType = (typeof TASK_EVENT_TYPES)[number]

export const TASK_DECISIONS = ['approve_spec', 'cancel', 'retry'] as const
export type TaskDecision = (typeof TASK_DECISIONS)[number]

export const RELEASE_DECISIONS = ['rollback', 'resume_releases'] as const
export type ReleaseDecision = (typeof RELEASE_DECISIONS)[number]

export const RELEASE_KINDS = ['release', 'rollback'] as const
export type ReleaseKind = (typeof RELEASE_KINDS)[number]

export const RELEASE_STATUSES = [
  'pending',
  'deploying',
  'verifying',
  'rolling_back',
  'released',
  'failed',
  'superseded',
  'revoked',
  'rolled_back',
] as const
export type ReleaseStatus = (typeof RELEASE_STATUSES)[number]

export const RELEASE_ORIGINS = ['task', 'auto_revert', 'resume', 'manual_rollback'] as const
export type ReleaseOrigin = (typeof RELEASE_ORIGINS)[number]

export interface OfficeSessionRequest {
  access_code: string
}

export interface Participant {
  id: string
  display_name: string
  role: Role
  expires_at: string
}

export interface SpecSource {
  title: string
  url: string
}

export interface Spec {
  version: number
  spec_hash: string
  summary: string
  acceptance_criteria: string[]
  sources: SpecSource[]
}

export interface Usage {
  model_calls: number
  model_calls_limit: number
  input_tokens: number
  output_tokens: number
}

export interface TaskCreate {
  text: string
  profile: ModelProfile
}

export interface PullRequestCheck {
  name: string
  status: string
  conclusion: string | null
  url: string | null
}

export interface PullRequestChecks {
  head_sha: string
  checks: PullRequestCheck[]
}

export interface PullRequestReviewItem {
  item: string
  ok: boolean
  note: string
}

export interface PullRequestReview {
  id: string
  head_sha: string
  verdict: 'approve' | 'reject'
  reasons: string
  required_changes: string[]
  checklist: PullRequestReviewItem[]
  created_at: string
}

export interface Task {
  id: string
  title: string
  text: string
  status: TaskStatus
  profile: ModelProfile
  owner_id: string
  owner_name: string
  version: number
  created_at: string
  updated_at: string
  rejection_reason: string | null
  spec: Spec | null
  branch: string | null
  base_sha: string | null
  pr_url: string | null
  pr_number: number | null
  head_sha: string | null
  failure_kind: FailureKind | null
  checks: PullRequestChecks | null
  pr_review: PullRequestReview | null
  merge_sha: string | null
  merge_pending: boolean
  integration_started: boolean
  changed_files: string[]
  fix_cycles: number
  usage: Usage
  rework_count: number
  rework_limit: number
  rework_reason: string | null
}

export interface TaskEvent {
  event_id: number
  task_id: string
  seq: number
  type: TaskEventType
  timestamp: string
  role: AgentRole | null
  tool: string | null
  artifact: string | null
  payload: Record<string, unknown>
}

export interface CheckResult {
  command: string
  exit_code: number
  duration_ms: number
  artifact_urls: string[]
}

export interface CheckReport {
  task_id: string
  commit_sha: string | null
  spec_hash: string | null
  suite_hash: string | null
  checks: CheckResult[]
}

export interface TaskDecisionRequest {
  decision: TaskDecision
  expected_version: number
  idempotency_key: string
}

export interface ReleaseDecisionRequest {
  decision: ReleaseDecision
  expected_version: number
  idempotency_key: string
  fix_sha?: string
  reason?: string
}

export interface Decision {
  decision_id: string
  decision: TaskDecision | ReleaseDecision
  task_id: string | null
  release_id: string | null
  participant_id: string
  expected_version: number
  idempotency_key: string
  recorded_at: string
}

export interface DeployResult {
  render_deploy_id: string
  render_status: string
  started_at: string
  finished_at: string | null
  published_sha: string | null
  error: string | null
}

export interface VerifyResult {
  expected_sha: string
  ok: boolean
  fault: boolean
  attempts: number
  health: boolean
  version: string | null
  page_version: string | null
  error: string | null
  checked_at: string
}

export interface RollbackResult {
  target_sha: string
  target_deploy_id: string
  render_deploy_id: string | null
  render_status: string | null
  started_at: string
  finished_at: string | null
  verify: VerifyResult | null
  error: string | null
}

export interface RevertResult {
  status: 'pending' | 'active' | 'needs_attention' | 'merged'
  source: 'auto' | 'resume'
  base_sha: string | null
  head_sha: string | null
  pr_number: number | null
  pr_url: string | null
  checks: PullRequestChecks | null
  fix_sha: string | null
  decision_id: string | null
  candidate_id: string | null
  reason: string | null
  attempt: number
  tree: string | null
  targets?: RevertTarget[]
}

export interface RevertTarget {
  release_id: string
  source: 'verify_failed' | 'rolled_back' | 'checks_failed'
  base_sha: string
  head_sha: string
  merges: string[] | null
  attempt: number | null
  fixed_by: string | null
  resumed: boolean
}

export interface ReleaseQueueState {
  configured: boolean
  active_release_id: string | null
  release_blocked_by: string | null
  blocked_reason: string | null
  published_sha: string | null
}

export interface ReleaseCandidate {
  id: string
  task_id: string
  kind: ReleaseKind
  status: ReleaseStatus
  version: number
  commit_sha: string
  expected_previous_sha: string
  spec_hash: string
  suite_hash: string
  ci_run_ids: string[]
  pr_url: string | null
  created_at: string
  is_published: boolean
  deploy: DeployResult | null
  pr_review_id: string | null
  e2e: CheckReport | null
  origin: ReleaseOrigin
  origin_decision_id: string | null
  superseded_by: string | null
  blocked_reason: string | null
  verify: VerifyResult | null
  rollback: RollbackResult | null
  revert: RevertResult | null
  reason?: string | null
}

export interface Slots {
  used: number
  total: number
}

export interface ApiError {
  code: string
  message: string
  retryable: boolean
  operation_id: string
}
