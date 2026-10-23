"""Схемы API по контракту `contracts/api.md`."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorCode = Literal[
    "unauthorized",
    "invalid_demo_code",
    "csrf_failed",
    "not_found",
    "validation_error",
    "idempotency_conflict",
    "run_not_active",
    "rate_limited",
    "quota_exceeded",
    "provider_unavailable",
    "provider_outcome_unknown",
    "response_truncated",
    "storage_unavailable",
    "run_cancelled",
    "service_unavailable",
    "internal_error",
]
MessageRole = Literal["user", "assistant", "system"]
SystemMessageKind = Literal["model_switched", "refusal", "check_unavailable"]
Intent = Literal["chat", "code", "image", "clarify"]
Complexity = Literal["trivial", "standard", "complex"]
Risk = Literal["none", "illegal", "internal_probe"]
Effort = Literal["low", "medium", "high", "unsupported"]
Provider = Literal["anthropic", "openai"]
ModelKind = Literal["text", "image"]
TextModelId = Literal["claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol"]
ModelId = Literal[TextModelId, "gpt-image-2.5-flare"]
ModelChoice = Literal["auto", "image", TextModelId]
SwitchReason = Literal["intent_mismatch", "provider_fallback"]
RunStatus = Literal[
    "queued", "running", "completed", "refused", "check_unavailable", "failed", "cancelled"
]
RunEventType = Literal[
    "accepted",
    "checks_passed",
    "refusal",
    "check_unavailable",
    "model_selected",
    "model_switched",
    "text_delta",
    "asset_ready",
    "completed",
    "failed",
]
HealthStatus = Literal["ok", "unavailable"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiError(Schema):
    code: ErrorCode
    message: str
    retryable: bool
    operation_id: str


class DemoSessionRequest(Schema):
    code: str = Field(min_length=1, max_length=128)


class Session(Schema):
    user_id: str
    expires_at: datetime
    csrf_token: str


class Conversation(Schema):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationList(Schema):
    items: list[Conversation]


class CreateConversationRequest(Schema):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class RenameConversationRequest(Schema):
    title: str = Field(min_length=1, max_length=200)


class AssetRef(Schema):
    id: str
    mime_type: str
    width: int | None
    height: int | None


class Message(Schema):
    id: str
    conversation_id: str
    role: MessageRole
    kind: SystemMessageKind | None
    text: str
    run_id: str | None
    effective_model: ModelId | None
    effort: Effort | None
    asset: AssetRef | None
    created_at: datetime


class CreateRunRequest(Schema):
    text: str = Field(min_length=1, max_length=8000)
    model: ModelChoice
    idempotency_key: str = Field(min_length=8, max_length=128)


class Run(Schema):
    id: str
    conversation_id: str
    status: RunStatus
    requested_model: ModelChoice
    effective_model: ModelId | None
    effort: Effort | None
    intent: Intent | None
    complexity: Complexity | None
    risk: Risk | None
    switch_reason: SwitchReason | None
    user_message_id: str
    assistant_message_id: str | None
    error: ApiError | None
    created_at: datetime
    updated_at: datetime


class MessageList(Schema):
    items: list[Message]
    active_run_id: str | None
    runs: list[Run]


class AcceptedPayload(Schema):
    user_message_id: str
    requested_model: ModelChoice


class ChecksPassedPayload(Schema):
    intent: Intent
    complexity: Complexity
    risk: Risk


class SystemMessagePayload(Schema):
    message: Message


class ModelSelectedPayload(Schema):
    assistant_message_id: str
    provider: Provider
    effective_model: ModelId
    effort: Effort


class ModelSwitchedPayload(Schema):
    requested_model: ModelChoice
    effective_model: ModelId
    switch_reason: SwitchReason
    message: Message


class TextDeltaPayload(Schema):
    message_id: str
    delta: str


class AssetReadyPayload(Schema):
    message_id: str
    asset: AssetRef


class CompletedPayload(Schema):
    message: Message


class FailedPayload(Schema):
    error: ApiError


class RunEventBase(Schema):
    event_id: str
    run_id: str
    seq: int = Field(ge=1)
    timestamp: datetime


class AcceptedEvent(RunEventBase):
    type: Literal["accepted"]
    payload: AcceptedPayload


class ChecksPassedEvent(RunEventBase):
    type: Literal["checks_passed"]
    payload: ChecksPassedPayload


class RefusalEvent(RunEventBase):
    type: Literal["refusal"]
    payload: SystemMessagePayload


class CheckUnavailableEvent(RunEventBase):
    type: Literal["check_unavailable"]
    payload: SystemMessagePayload


class ModelSelectedEvent(RunEventBase):
    type: Literal["model_selected"]
    payload: ModelSelectedPayload


class ModelSwitchedEvent(RunEventBase):
    type: Literal["model_switched"]
    payload: ModelSwitchedPayload


class TextDeltaEvent(RunEventBase):
    type: Literal["text_delta"]
    payload: TextDeltaPayload


class AssetReadyEvent(RunEventBase):
    type: Literal["asset_ready"]
    payload: AssetReadyPayload


class CompletedEvent(RunEventBase):
    type: Literal["completed"]
    payload: CompletedPayload


class FailedEvent(RunEventBase):
    type: Literal["failed"]
    payload: FailedPayload


RunEvent = Annotated[
    AcceptedEvent
    | ChecksPassedEvent
    | RefusalEvent
    | CheckUnavailableEvent
    | ModelSelectedEvent
    | ModelSwitchedEvent
    | TextDeltaEvent
    | AssetReadyEvent
    | CompletedEvent
    | FailedEvent,
    Field(discriminator="type"),
]


class AssetUrl(Schema):
    url: str
    expires_at: datetime


class ModelProfile(Schema):
    id: ModelId
    provider: Provider
    label: str
    kind: ModelKind
    selectable: bool


class ModelCatalog(Schema):
    items: list[ModelProfile]


class VersionInfo(Schema):
    version: str


class Health(Schema):
    status: HealthStatus
