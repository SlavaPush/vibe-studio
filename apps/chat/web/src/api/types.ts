export type ErrorCode =
  | 'unauthorized'
  | 'invalid_demo_code'
  | 'csrf_failed'
  | 'not_found'
  | 'validation_error'
  | 'idempotency_conflict'
  | 'run_not_active'
  | 'rate_limited'
  | 'quota_exceeded'
  | 'provider_unavailable'
  | 'provider_outcome_unknown'
  | 'storage_unavailable'
  | 'run_cancelled'
  | 'service_unavailable'
  | 'internal_error'
export type MessageRole = 'user' | 'assistant' | 'system'
export type SystemMessageKind = 'model_switched' | 'refusal' | 'check_unavailable'
export type Intent = 'chat' | 'code' | 'image' | 'clarify'
export type Complexity = 'trivial' | 'standard' | 'complex'
export type Risk = 'none' | 'illegal' | 'internal_probe'
export type Effort = 'low' | 'medium' | 'high' | 'unsupported'
export type Provider = 'anthropic' | 'openai'
export type ModelKind = 'text' | 'image'
export type TextModelId = 'claude-haiku-5-5' | 'claude-sonnet-5-5' | 'gpt-6-luna' | 'gpt-6.1-sol'
export type ModelId = TextModelId | 'gpt-image-2.5-flare'
export type ModelChoice = 'auto' | 'image' | TextModelId
export type SwitchReason = 'intent_mismatch' | 'provider_fallback'
export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'refused'
  | 'check_unavailable'
  | 'failed'
  | 'cancelled'
export type RunEventType = RunEvent['type']
export type HealthStatus = 'ok' | 'unavailable'

export interface ApiError {
  code: ErrorCode
  message: string
  retryable: boolean
  operation_id: string
}

export interface DemoSessionRequest {
  code: string
}

export interface Session {
  user_id: string
  expires_at: string
  csrf_token: string
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ConversationList {
  items: Conversation[]
}

export interface CreateConversationRequest {
  title?: string | null
}

export interface RenameConversationRequest {
  title: string
}

export interface AssetRef {
  id: string
  mime_type: string
  width: number | null
  height: number | null
}

export interface Message {
  id: string
  conversation_id: string
  role: MessageRole
  kind: SystemMessageKind | null
  text: string
  run_id: string | null
  effective_model: ModelId | null
  effort: Effort | null
  asset: AssetRef | null
  created_at: string
}

export interface MessageList {
  items: Message[]
  active_run_id: string | null
}

export interface CreateRunRequest {
  text: string
  model: ModelChoice
  idempotency_key: string
}

export interface Run {
  id: string
  conversation_id: string
  status: RunStatus
  requested_model: ModelChoice
  effective_model: ModelId | null
  effort: Effort | null
  intent: Intent | null
  complexity: Complexity | null
  risk: Risk | null
  switch_reason: SwitchReason | null
  user_message_id: string
  assistant_message_id: string | null
  error: ApiError | null
  created_at: string
  updated_at: string
}

export interface AcceptedPayload {
  user_message_id: string
  requested_model: ModelChoice
}

export interface ChecksPassedPayload {
  intent: Intent
  complexity: Complexity
  risk: Risk
}

export interface SystemMessagePayload {
  message: Message
}

export interface ModelSelectedPayload {
  assistant_message_id: string
  provider: Provider
  effective_model: ModelId
  effort: Effort
}

export interface ModelSwitchedPayload {
  requested_model: ModelChoice
  effective_model: ModelId
  switch_reason: SwitchReason
  message: Message
}

export interface TextDeltaPayload {
  message_id: string
  delta: string
}

export interface AssetReadyPayload {
  message_id: string
  asset: AssetRef
}

export interface CompletedPayload {
  message: Message
}

export interface FailedPayload {
  error: ApiError
}

export interface RunEventBase {
  event_id: string
  run_id: string
  seq: number
  timestamp: string
}

export type AcceptedEvent = RunEventBase & { type: 'accepted'; payload: AcceptedPayload }
export type ChecksPassedEvent = RunEventBase & { type: 'checks_passed'; payload: ChecksPassedPayload }
export type RefusalEvent = RunEventBase & { type: 'refusal'; payload: SystemMessagePayload }
export type CheckUnavailableEvent = RunEventBase & {
  type: 'check_unavailable'
  payload: SystemMessagePayload
}
export type ModelSelectedEvent = RunEventBase & {
  type: 'model_selected'
  payload: ModelSelectedPayload
}
export type ModelSwitchedEvent = RunEventBase & {
  type: 'model_switched'
  payload: ModelSwitchedPayload
}
export type TextDeltaEvent = RunEventBase & { type: 'text_delta'; payload: TextDeltaPayload }
export type AssetReadyEvent = RunEventBase & { type: 'asset_ready'; payload: AssetReadyPayload }
export type CompletedEvent = RunEventBase & { type: 'completed'; payload: CompletedPayload }
export type FailedEvent = RunEventBase & { type: 'failed'; payload: FailedPayload }

export type RunEvent =
  | AcceptedEvent
  | ChecksPassedEvent
  | RefusalEvent
  | CheckUnavailableEvent
  | ModelSelectedEvent
  | ModelSwitchedEvent
  | TextDeltaEvent
  | AssetReadyEvent
  | CompletedEvent
  | FailedEvent

export interface AssetUrl {
  url: string
  expires_at: string
}

export interface ModelProfile {
  id: ModelId
  provider: Provider
  label: string
  kind: ModelKind
  selectable: boolean
}

export interface ModelCatalog {
  items: ModelProfile[]
}

export interface VersionInfo {
  version: string
}

export interface Health {
  status: HealthStatus
}
