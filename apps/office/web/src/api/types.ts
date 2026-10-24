export const ROLES = ['participant', 'admin'] as const
export type Role = (typeof ROLES)[number]

export const TASK_STATUSES = [
  'rejected',
  'queued',
  'awaiting_spec_approval',
  'developing',
  'reviewing',
  'awaiting_integration',
  'awaiting_release_approval',
  'deploying',
  'released',
  'failed',
  'cancelled',
  'needs_attention',
] as const
export type TaskStatus = (typeof TASK_STATUSES)[number]

export const SLOT_STATUSES: readonly TaskStatus[] = [
  'awaiting_spec_approval',
  'developing',
  'reviewing',
  'awaiting_integration',
  'awaiting_release_approval',
  'deploying',
]

export const SLOTS_TOTAL = 3

export const MODEL_PROFILES = ['local', 'cloud', 'hybrid'] as const
export type ModelProfile = (typeof MODEL_PROFILES)[number]

export const AGENT_ROLES = ['supervisor', 'researcher', 'developer', 'qa', 'critic'] as const
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
] as const
export type TaskEventType = (typeof TASK_EVENT_TYPES)[number]

export const TASK_DECISIONS = ['approve_spec', 'cancel', 'retry'] as const
export type TaskDecision = (typeof TASK_DECISIONS)[number]

export const RELEASE_DECISIONS = ['approve_release', 'rollback'] as const
export type ReleaseDecision = (typeof RELEASE_DECISIONS)[number]

export const RELEASE_KINDS = ['release', 'rollback'] as const
export type ReleaseKind = (typeof RELEASE_KINDS)[number]

export const RELEASE_STATUSES = [
  'pending',
  'approved',
  'deploying',
  'released',
  'failed',
  'rolled_back',
  'revoked',
] as const
export type ReleaseStatus = (typeof RELEASE_STATUSES)[number]

export interface OfficeSessionRequest {
  access_code: string
  display_name: string
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
  changed_files: string[]
  fix_cycles: number
  usage: Usage
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
  approved_by: string | null
  approved_at: string | null
  is_published: boolean
  deploy: DeployResult | null
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
