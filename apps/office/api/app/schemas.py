from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    participant = "participant"
    admin = "admin"


class TaskStatus(StrEnum):
    rejected = "rejected"
    queued = "queued"
    awaiting_spec_approval = "awaiting_spec_approval"
    developing = "developing"
    reviewing = "reviewing"
    awaiting_integration = "awaiting_integration"
    awaiting_release_approval = "awaiting_release_approval"
    deploying = "deploying"
    released = "released"
    failed = "failed"
    cancelled = "cancelled"
    needs_attention = "needs_attention"


SLOT_STATUSES = frozenset(
    {
        TaskStatus.awaiting_spec_approval,
        TaskStatus.developing,
        TaskStatus.reviewing,
        TaskStatus.awaiting_integration,
        TaskStatus.awaiting_release_approval,
        TaskStatus.deploying,
    }
)

SLOTS_TOTAL = 3


class ModelProfile(StrEnum):
    local = "local"
    cloud = "cloud"
    hybrid = "hybrid"


class AgentRole(StrEnum):
    supervisor = "supervisor"
    researcher = "researcher"
    developer = "developer"
    qa = "qa"
    critic = "critic"


class TaskEventType(StrEnum):
    task_created = "task_created"
    task_rejected = "task_rejected"
    status_changed = "status_changed"
    role_started = "role_started"
    role_finished = "role_finished"
    tool_called = "tool_called"
    artifact_ready = "artifact_ready"
    decision_required = "decision_required"
    decision_recorded = "decision_recorded"
    check_result = "check_result"
    release_candidate = "release_candidate"
    deploy_status = "deploy_status"


class TaskDecision(StrEnum):
    approve_spec = "approve_spec"
    cancel = "cancel"
    retry = "retry"


class ReleaseDecision(StrEnum):
    approve_release = "approve_release"
    rollback = "rollback"


class ReleaseKind(StrEnum):
    release = "release"
    rollback = "rollback"


class ReleaseStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    deploying = "deploying"
    released = "released"
    failed = "failed"
    rolled_back = "rolled_back"
    revoked = "revoked"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfficeSessionRequest(ContractModel):
    access_code: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=40)


class Participant(ContractModel):
    id: str
    display_name: str = Field(min_length=1, max_length=40)
    role: Role
    expires_at: datetime


class SpecSource(ContractModel):
    title: str
    url: str


class Spec(ContractModel):
    version: int = Field(ge=1)
    spec_hash: str
    summary: str
    acceptance_criteria: list[str]
    sources: list[SpecSource]


class Usage(ContractModel):
    model_calls: int = Field(ge=0)
    model_calls_limit: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class TaskCreate(ContractModel):
    text: str = Field(min_length=20, max_length=4000)
    profile: ModelProfile


class Task(ContractModel):
    id: str
    title: str = Field(max_length=80)
    text: str
    status: TaskStatus
    profile: ModelProfile
    owner_id: str
    owner_name: str
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    rejection_reason: str | None
    spec: Spec | None
    branch: str | None
    base_sha: str | None
    pr_url: str | None
    changed_files: list[str]
    fix_cycles: int = Field(ge=0, le=2)
    usage: Usage


class TaskEvent(ContractModel):
    event_id: int = Field(ge=1)
    task_id: str
    seq: int = Field(ge=1)
    type: TaskEventType
    timestamp: datetime
    role: AgentRole | None
    tool: str | None
    artifact: str | None
    payload: dict[str, Any]


class CheckResult(ContractModel):
    command: str
    exit_code: int
    duration_ms: int = Field(ge=0)
    artifact_urls: list[str]


class CheckReport(ContractModel):
    task_id: str
    commit_sha: str | None
    spec_hash: str | None
    suite_hash: str | None
    checks: list[CheckResult]


class TaskDecisionRequest(ContractModel):
    decision: TaskDecision
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=64)


class ReleaseDecisionRequest(ContractModel):
    decision: ReleaseDecision
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=64)


class Decision(ContractModel):
    decision_id: str
    decision: TaskDecision | ReleaseDecision
    task_id: str | None
    release_id: str | None
    participant_id: str
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=64)
    recorded_at: datetime


class DeployResult(ContractModel):
    render_deploy_id: str
    render_status: str
    started_at: datetime
    finished_at: datetime | None
    published_sha: str | None
    error: str | None


class ReleaseCandidate(ContractModel):
    id: str
    task_id: str
    kind: ReleaseKind
    status: ReleaseStatus
    version: int = Field(ge=1)
    commit_sha: str
    expected_previous_sha: str
    spec_hash: str
    suite_hash: str
    ci_run_ids: list[str]
    pr_url: str | None
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    is_published: bool
    deploy: DeployResult | None


class Slots(ContractModel):
    used: int = Field(ge=0)
    total: int = Field(ge=1)


class ApiError(ContractModel):
    code: str
    message: str
    retryable: bool
    operation_id: str


def count_slots(tasks: list[Task]) -> int:
    return sum(1 for task in tasks if task.status in SLOT_STATUSES)
