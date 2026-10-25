from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

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
    awaiting_release = "awaiting_release"
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
    }
)

WORKTREE_STATUSES = SLOT_STATUSES | frozenset(
    {
        TaskStatus.awaiting_integration,
        TaskStatus.awaiting_release,
        TaskStatus.deploying,
        TaskStatus.failed,
        TaskStatus.needs_attention,
    }
)

SLOTS_TOTAL = 3


class FailureKind(StrEnum):
    checks_failed = "checks_failed"
    pr_rejected = "pr_rejected"
    deploy_failed = "deploy_failed"
    verify_failed = "verify_failed"
    rolled_back = "rolled_back"
    owner_rejected = "owner_rejected"


class ChangeClass(StrEnum):
    frontend_ui = "frontend_ui"
    backend_api = "backend_api"


class Contour(StrEnum):
    ui = "ui"
    server = "server"


REWORK_SOURCES = frozenset({FailureKind.verify_failed, FailureKind.rolled_back, FailureKind.checks_failed})


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
    pr_reviewer = "pr_reviewer"


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
    pr_opened = "pr_opened"
    checks_status = "checks_status"
    pr_closed = "pr_closed"
    operation_unknown = "operation_unknown"
    pr_review = "pr_review"
    merge_started = "merge_started"
    pr_merged = "pr_merged"
    rollback_status = "rollback_status"
    release_blocked = "release_blocked"
    release_unblocked = "release_unblocked"
    external_publish = "external_publish"
    rework_started = "rework_started"
    owner_review_requested = "owner_review_requested"
    owner_review = "owner_review"


class TaskDecision(StrEnum):
    approve_spec = "approve_spec"
    cancel = "cancel"
    retry = "retry"


class ReleaseDecision(StrEnum):
    rollback = "rollback"
    resume_releases = "resume_releases"


class ReleaseKind(StrEnum):
    release = "release"
    rollback = "rollback"


class ReleaseStatus(StrEnum):
    pending = "pending"
    deploying = "deploying"
    verifying = "verifying"
    rolling_back = "rolling_back"
    released = "released"
    failed = "failed"
    superseded = "superseded"
    revoked = "revoked"
    rolled_back = "rolled_back"


ACTIVE_RELEASE_STATUSES = frozenset({ReleaseStatus.deploying, ReleaseStatus.verifying, ReleaseStatus.rolling_back})


class ReleaseOrigin(StrEnum):
    task = "task"
    auto_revert = "auto_revert"
    resume = "resume"
    manual_rollback = "manual_rollback"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfficeSessionRequest(ContractModel):
    access_code: str = Field(min_length=1)


class Participant(ContractModel):
    id: str
    display_name: str = Field(min_length=1)
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


class PullRequestCheck(ContractModel):
    name: str
    status: str
    conclusion: str | None
    url: str | None


class PullRequestChecks(ContractModel):
    head_sha: str
    checks: list[PullRequestCheck]


class PullRequestReviewItem(ContractModel):
    item: str
    ok: bool
    note: str


class PullRequestReview(ContractModel):
    id: str
    head_sha: str
    verdict: Literal["approve", "reject"]
    reasons: str
    required_changes: list[str]
    checklist: list[PullRequestReviewItem]
    created_at: datetime
    owner_summary: str | None = None


class OwnerReview(ContractModel):
    state: Literal["requested", "approved", "changes_requested"]
    head_sha: str
    login: str | None = None
    comment: str | None = None


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
    pr_number: int | None
    head_sha: str | None
    failure_kind: FailureKind | None
    checks: PullRequestChecks | None
    pr_review: PullRequestReview | None
    merge_sha: str | None
    merge_pending: bool
    integration_started: bool
    changed_files: list[str]
    fix_cycles: int = Field(ge=0, le=2)
    usage: Usage
    rework_count: int = Field(ge=0)
    rework_limit: int = Field(ge=0)
    rework_reason: str | None
    change_class: ChangeClass = ChangeClass.frontend_ui
    contour: Contour | None = None
    owner_review: OwnerReview | None = None


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
    fix_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    reason: str | None = Field(default=None, max_length=500)


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


class VerifyResult(ContractModel):
    expected_sha: str
    ok: bool
    fault: bool
    attempts: int = Field(ge=0)
    health: bool
    version: str | None
    page_version: str | None
    error: str | None
    checked_at: datetime


class RollbackResult(ContractModel):
    target_sha: str
    target_deploy_id: str
    render_deploy_id: str | None
    render_status: str | None
    started_at: datetime
    finished_at: datetime | None
    verify: VerifyResult | None
    error: str | None


class RevertTarget(ContractModel):
    release_id: str
    source: Literal["verify_failed", "rolled_back", "checks_failed"]
    base_sha: str
    head_sha: str
    merges: list[str] | None = None
    attempt: int | None = None
    fixed_by: str | None = None
    resumed: bool = False


class RevertResult(ContractModel):
    status: Literal["pending", "active", "needs_attention", "merged"]
    source: Literal["auto", "resume"]
    base_sha: str | None
    head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    checks: PullRequestChecks | None
    fix_sha: str | None
    decision_id: str | None
    candidate_id: str | None
    reason: str | None
    attempt: int = Field(default=1, ge=1)
    tree: str | None = None
    targets: list[RevertTarget] = Field(default_factory=list)
    contour: Contour | None = None
    owner_review: OwnerReview | None = None


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
    is_published: bool
    deploy: DeployResult | None
    pr_review_id: str | None
    e2e: CheckReport | None
    origin: ReleaseOrigin
    origin_decision_id: str | None
    superseded_by: str | None
    blocked_reason: str | None
    verify: VerifyResult | None
    rollback: RollbackResult | None
    revert: RevertResult | None
    reason: str | None = None


class ReleaseQueueState(ContractModel):
    configured: bool
    active_release_id: str | None
    release_blocked_by: str | None
    blocked_reason: str | None
    published_sha: str | None


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
