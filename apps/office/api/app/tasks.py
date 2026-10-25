import json
import logging
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.db import Connection, Database, from_text, to_text, utcnow
from app.errors import not_found, rate_limited
from app.schemas import (
    REWORK_SOURCES,
    SLOT_STATUSES,
    SLOTS_TOTAL,
    WORKTREE_STATUSES,
    AgentRole,
    ChangeClass,
    CheckReport,
    Contour,
    FailureKind,
    PullRequestReview,
    ReleaseCandidate,
    ReleaseKind,
    ReleaseOrigin,
    ReleaseStatus,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)

if TYPE_CHECKING:
    from app.models_gateway import Target

logger = logging.getLogger("office.tasks")

LEGACY_PROFILE = "cloud"
MODEL_CALLS_LIMITS = {ChangeClass.frontend_ui: 30, ChangeClass.backend_api: 60}
SIGNAL_SLOT_ACQUIRED = "slot_acquired"
SIGNAL_DECISION_RECORDED = "decision_recorded"
SIGNAL_TASK_CLOSED = "task_closed"
SIGNAL_SLOT_RELEASED = "slot_released"
CLOSED_STATUSES = frozenset({TaskStatus.cancelled, TaskStatus.released})

S = TaskStatus
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.rejected: frozenset(),
    S.queued: frozenset({S.awaiting_spec_approval, S.developing, S.cancelled}),
    S.awaiting_spec_approval: frozenset({S.developing, S.cancelled, S.failed, S.needs_attention}),
    S.developing: frozenset({S.reviewing, S.cancelled, S.failed, S.needs_attention}),
    S.reviewing: frozenset(
        {S.developing, S.awaiting_integration, S.cancelled, S.failed, S.needs_attention}
    ),
    S.awaiting_integration: frozenset({S.awaiting_release, S.cancelled, S.failed, S.needs_attention}),
    S.awaiting_release: frozenset({S.deploying, S.released, S.failed, S.needs_attention}),
    S.deploying: frozenset({S.released, S.failed, S.needs_attention}),
    S.released: frozenset(),
    S.failed: frozenset({S.queued}),
    S.cancelled: frozenset(),
    S.needs_attention: frozenset(
        {S.queued, S.awaiting_integration, S.awaiting_release, S.released, S.failed, S.cancelled}
    ),
}

TASK_SELECT = (
    "SELECT t.*, p.display_name AS owner_name, r.id AS review_id, r.head_sha AS review_head_sha, "
    "r.verdict AS review_verdict, r.reasons AS review_reasons, r.required_changes AS review_required_changes, "
    "r.checklist AS review_checklist, r.created_at AS review_created_at, r.owner_summary AS review_owner_summary, "
    "EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id AND o.kind = 'merge_pr' AND o.release_id IS NULL "
    "AND o.status IN ('started', 'unknown')) AS merge_pending, "
    "EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id AND (t.rework_count = 0 "
    "OR o.key LIKE ('%:' || t.id || '-r' || t.rework_count || ':%') "
    "OR o.key LIKE ('%:' || t.id || '-r' || t.rework_count))) AS has_operations FROM tasks t "
    "JOIN participants p ON p.id = t.owner_id "
    "LEFT JOIN pr_reviews r ON r.id = (SELECT x.id FROM pr_reviews x WHERE x.task_id = t.id "
    "AND (t.head_sha IS NULL OR x.head_sha = t.head_sha) ORDER BY x.created_at DESC, x.id DESC LIMIT 1)"
)
PR_REVIEW_CALLS_LIMIT = 2
REWORK_LIMIT = 1
REWORK_MATERIALS = ("reason", "deploy_events", "render_logs", "actions_log", "source_diff")
LIMIT_REACHED = "Лимит доработки исчерпан: повторной доработки не будет."
REWORK_RESET = (
    "rework_priority = :no, branch = NULL, base_sha = NULL, head_sha = NULL, pr_number = NULL, pr_url = NULL, "
    "merge_sha = NULL, checks = NULL, failure_kind = NULL, changed_files = '[]', fix_cycles = 0, pr_review_calls = 0, "
    "contour = NULL, owner_review = NULL"
)

SignalHandler = Callable[[str, str], None]


class TransitionError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class IntakeOutcome:
    accepted: bool
    reason: str | None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    change_class: ChangeClass = ChangeClass.frontend_ui
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    cost_usd: float = 0.0
    meter: Any = field(default=None, compare=False, repr=False)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def rework_tree(task_id: str, count: int) -> str:
    return f"{task_id}-r{count}" if count else task_id


def task_title(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip()[:80] if lines else ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def task_from_row(row: dict[str, Any], rework_limit: int = REWORK_LIMIT) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        text=row["text"],
        status=row["status"],
        owner_id=row["owner_id"],
        owner_name=row["owner_name"],
        version=row["version"],
        created_at=from_text(row["created_at"]),
        updated_at=from_text(row["updated_at"]),
        rejection_reason=row["rejection_reason"],
        spec=json.loads(row["spec"]) if row["spec"] else None,
        branch=row["branch"],
        base_sha=row["base_sha"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        failure_kind=row["failure_kind"],
        checks=json.loads(row["checks"]) if row["checks"] else None,
        pr_review=review_from_row(row) if row.get("review_id") else None,
        merge_sha=row["merge_sha"],
        merge_pending=bool(row["merge_pending"]),
        integration_started=bool(
            row["head_sha"] or row["pr_number"] is not None or row["merge_sha"] or row["has_operations"]
        ),
        changed_files=json.loads(row["changed_files"]),
        fix_cycles=row["fix_cycles"],
        usage={
            "model_calls": row["model_calls"],
            "model_calls_limit": row["model_calls_limit"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cost_usd": round(max(0.0, row["cost_usd"]), 4),
        },
        rework_count=row["rework_count"],
        rework_limit=rework_limit,
        rework_reason=row["rework_reason"],
        change_class=row["change_class"],
        contour=row["contour"],
        owner_review=json.loads(row["owner_review"]) if row["owner_review"] else None,
    )


def review_from_row(row: dict[str, Any]) -> PullRequestReview:
    return PullRequestReview(
        id=row["review_id"],
        head_sha=row["review_head_sha"],
        verdict=row["review_verdict"],
        reasons=row["review_reasons"],
        required_changes=json.loads(row["review_required_changes"]),
        checklist=json.loads(row["review_checklist"]),
        created_at=from_text(row["review_created_at"]),
        owner_summary=row["review_owner_summary"],
    )


def event_from_row(row: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        event_id=row["event_id"],
        task_id=row["task_id"],
        seq=row["seq"],
        type=row["type"],
        timestamp=from_text(row["occurred_at"]),
        role=row["role"],
        tool=row["tool"],
        artifact=row["artifact"],
        payload=json.loads(row["payload"]),
    )


def candidate_from_row(row: dict[str, Any]) -> ReleaseCandidate:
    return ReleaseCandidate(
        id=row["id"],
        task_id=row["task_id"],
        kind=row["kind"],
        status=row["status"],
        version=row["version"],
        commit_sha=row["commit_sha"],
        expected_previous_sha=row["expected_previous_sha"],
        spec_hash=row["spec_hash"],
        suite_hash=row["suite_hash"],
        ci_run_ids=json.loads(row["ci_run_ids"]),
        pr_url=row["pr_url"],
        created_at=from_text(row["created_at"]),
        is_published=bool(row["is_published"]),
        deploy=json.loads(row["deploy"]) if row["deploy"] else None,
        pr_review_id=row["pr_review_id"],
        e2e=json.loads(row["e2e"]) if row["e2e"] else None,
        origin=row["origin"],
        origin_decision_id=row["origin_decision_id"],
        superseded_by=row["superseded_by"],
        blocked_reason=row["blocked_reason"],
        verify=json.loads(row["verify"]) if row["verify"] else None,
        rollback=json.loads(row["rollback"]) if row["rollback"] else None,
        revert=json.loads(row["revert"]) if row["revert"] else None,
        reason=row["reason"],
    )


class TaskStore:
    def __init__(
        self, db: Database, notify: Callable[[], None] = lambda: None, pr_review_calls: int = PR_REVIEW_CALLS_LIMIT,
        rework_limit: int = REWORK_LIMIT,
    ) -> None:
        self.db = db
        self.notify = notify
        self.pr_review_calls = pr_review_calls
        self.rework_limit = rework_limit
        self.signal_handlers: list[SignalHandler] = []

    def signal(self, conn: Connection, name: str, task_id: str) -> None:
        conn.after_commit(partial(self._emit, name, task_id))

    def _emit(self, name: str, task_id: str) -> None:
        for handler in self.signal_handlers:
            try:
                handler(name, task_id)
            except Exception:
                logger.exception("signal handler failed: %s %s", name, task_id)

    def load_task(self, conn: Connection, task_id: str) -> Task | None:
        row = conn.fetchone(f"{TASK_SELECT} WHERE t.id = :id", {"id": task_id})
        return None if row is None else task_from_row(row, self.rework_limit)

    def get_task(self, task_id: str) -> Task:
        with self.db.connection() as conn:
            task = self.load_task(conn, task_id)
        if task is None:
            raise not_found("Задача не найдена.")
        return task

    def list_tasks(self) -> list[Task]:
        with self.db.connection() as conn:
            rows = conn.fetchall(f"{TASK_SELECT} ORDER BY t.created_at DESC, t.id DESC")
        return [task_from_row(row, self.rework_limit) for row in rows]

    def slots_used(self, conn: Connection | None = None) -> int:
        statuses = {f"s{index}": status.value for index, status in enumerate(SLOT_STATUSES)}
        sql = f"SELECT COUNT(*) AS used FROM tasks WHERE status IN ({', '.join(':' + key for key in statuses)})"
        if conn is not None:
            return conn.fetchone(sql, statuses)["used"]
        with self.db.connection() as own:
            return own.fetchone(sql, statuses)["used"]

    def record_event(
        self,
        conn: Connection,
        task_id: str,
        event_type: TaskEventType,
        payload: dict[str, Any],
        *,
        role: AgentRole | None = None,
        tool: str | None = None,
        artifact: str | None = None,
    ) -> TaskEvent:
        if not conn.locked:
            raise RuntimeError("record_event requires locked_transaction")
        event_id = conn.fetchone("SELECT next_event_id FROM scheduler WHERE id = 1")["next_event_id"]
        conn.execute("UPDATE scheduler SET next_event_id = :next WHERE id = 1", {"next": event_id + 1})
        seq = conn.fetchone("SELECT event_seq FROM tasks WHERE id = :id", {"id": task_id})["event_seq"] + 1
        conn.execute("UPDATE tasks SET event_seq = :seq WHERE id = :id", {"seq": seq, "id": task_id})
        row = {
            "event_id": event_id,
            "task_id": task_id,
            "seq": seq,
            "type": event_type.value,
            "occurred_at": to_text(utcnow()),
            "role": role.value if role else None,
            "tool": tool,
            "artifact": artifact,
            "payload": _json(payload),
        }
        conn.execute(
            "INSERT INTO task_events (event_id, task_id, seq, type, occurred_at, role, tool, artifact, payload) "
            "VALUES (:event_id, :task_id, :seq, :type, :occurred_at, :role, :tool, :artifact, :payload)",
            row,
        )
        conn.after_commit(self.notify)
        return event_from_row(row)

    def transition(
        self, conn: Connection, task_id: str, to: TaskStatus, *, expected_from: TaskStatus
    ) -> Task:
        if not conn.locked:
            raise RuntimeError("transition requires locked_transaction")
        task = self.load_task(conn, task_id)
        if task is None:
            raise not_found("Задача не найдена.")
        if task.status != expected_from:
            raise TransitionError(f"{task_id}: status {task.status}, expected {expected_from}")
        if to not in TRANSITIONS[task.status]:
            raise TransitionError(f"{task_id}: {task.status} -> {to} is not allowed")
        conn.execute(
            "UPDATE tasks SET status = :status, version = version + 1, updated_at = :now WHERE id = :id",
            {"status": to.value, "now": to_text(utcnow()), "id": task_id},
        )
        self.record_event(
            conn, task_id, TaskEventType.status_changed, {"from": task.status.value, "to": to.value}
        )
        if task.status in SLOT_STATUSES and to not in SLOT_STATUSES:
            self.signal(conn, SIGNAL_SLOT_RELEASED, task_id)
            self._promote(conn)
        if to in SLOT_STATUSES and task.status not in SLOT_STATUSES:
            self.signal(conn, SIGNAL_SLOT_ACQUIRED, task_id)
        if to in CLOSED_STATUSES:
            self.signal(conn, SIGNAL_TASK_CLOSED, task_id)
        return self.load_task(conn, task_id)

    def add_event(
        self,
        task_id: str,
        event_type: TaskEventType,
        payload: dict[str, Any],
        *,
        role: AgentRole | None = None,
        tool: str | None = None,
        artifact: str | None = None,
    ) -> TaskEvent:
        with self.db.locked_transaction() as conn:
            return self.record_event(
                conn, task_id, event_type, payload, role=role, tool=tool, artifact=artifact
            )

    def fail_task(self, task_id: str, *, tool: str, arguments: str, reason: str) -> None:
        with self.db.locked_transaction() as conn:
            task = self.load_task(conn, task_id)
            if task is None or task.status not in SLOT_STATUSES:
                return
            self.record_event(
                conn,
                task_id,
                TaskEventType.tool_called,
                {"arguments": arguments, "result": "error", "reason": reason},
                tool=tool,
            )
            self.transition(conn, task_id, TaskStatus.failed, expected_from=task.status)

    def record_worktree(self, task_id: str, branch: str, base_sha: str, created: bool) -> bool:
        with self.db.locked_transaction() as conn:
            task = self.load_task(conn, task_id)
            if task is None or task.status not in WORKTREE_STATUSES:
                return False
            if (task.branch, task.base_sha) != (branch, base_sha):
                conn.execute(
                    "UPDATE tasks SET branch = :branch, base_sha = :base_sha, version = version + 1, "
                    "updated_at = :now WHERE id = :id",
                    {"branch": branch, "base_sha": base_sha, "now": to_text(utcnow()), "id": task_id},
                )
            if created or (task.branch, task.base_sha) != (branch, base_sha):
                self.record_event(
                    conn,
                    task_id,
                    TaskEventType.tool_called,
                    {"arguments": f"{branch} {base_sha}", "result": "ok"},
                    tool="git_worktree",
                )
            return True

    def record_commit(
        self, task_id: str, sha: str, changed_files: list[str], role: AgentRole | None = None,
        contour: Contour = Contour.ui,
    ) -> Task:
        with self.db.locked_transaction() as conn:
            conn.execute(
                "UPDATE tasks SET changed_files = :files, contour = :contour, version = version + 1, updated_at = :now "
                "WHERE id = :id",
                {"files": _json(changed_files), "contour": contour.value, "now": to_text(utcnow()), "id": task_id},
            )
            self.record_event(
                conn,
                task_id,
                TaskEventType.tool_called,
                {"arguments": f"{sha} ({len(changed_files)} файлов)", "result": "ok"},
                role=role,
                tool="git_commit",
            )
            return self.load_task(conn, task_id)

    def reserve_model_call(self, task_id: str, role: AgentRole, step: str, target: "Target") -> None:
        with self.db.locked_transaction() as conn:
            task = self.load_task(conn, task_id)
            if task is None:
                raise not_found("Задача не найдена.")
            if role != AgentRole.pr_reviewer:
                if task.usage.model_calls >= task.usage.model_calls_limit:
                    raise BudgetExceeded(f"{task_id}: {task.usage.model_calls_limit} model calls used")
                conn.execute(
                    "UPDATE tasks SET model_calls = model_calls + 1, version = version + 1, updated_at = :now "
                    "WHERE id = :id",
                    {"now": to_text(utcnow()), "id": task_id},
                )
            self.record_event(
                conn,
                task_id,
                TaskEventType.tool_called,
                {
                    "arguments": f"{role.value}/{step} {target.name}",
                    "result": "ok",
                    "provider": target.provider,
                    "model": target.model,
                    "effort": target.effort,
                },
                role=role,
                tool="model",
            )

    def reserve_pr_review(self, task_id: str, limit: int) -> None:
        with self.db.locked_transaction() as conn:
            changed = conn.execute(
                "UPDATE tasks SET pr_review_calls = pr_review_calls + 1, version = version + 1, updated_at = :now "
                "WHERE id = :id AND pr_review_calls < :limit",
                {"now": to_text(utcnow()), "id": task_id, "limit": limit},
            )
        if changed != 1:
            raise BudgetExceeded(f"{task_id}: {limit} pr review calls used")

    def pr_review_calls_used(self, conn: Connection, task_id: str) -> int:
        return conn.fetchone("SELECT pr_review_calls FROM tasks WHERE id = :id", {"id": task_id})["pr_review_calls"]

    def load_review(self, conn: Connection, task_id: str, head_sha: str | None) -> PullRequestReview | None:
        row = conn.fetchone(
            "SELECT id AS review_id, head_sha AS review_head_sha, verdict AS review_verdict, "
            "reasons AS review_reasons, required_changes AS review_required_changes, checklist AS review_checklist, "
            "created_at AS review_created_at, owner_summary AS review_owner_summary FROM pr_reviews "
            "WHERE task_id = :id AND head_sha = :head",
            {"id": task_id, "head": head_sha},
        )
        return None if row is None else review_from_row(row)

    def has_review(self, conn: Connection, task_id: str, head_sha: str | None) -> bool:
        return self.load_review(conn, task_id, head_sha) is not None

    def record_usage(self, task_id: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        with self.db.locked_transaction() as conn:
            conn.execute(
                "UPDATE tasks SET input_tokens = input_tokens + :input, output_tokens = output_tokens + :output, "
                "cost_usd = cost_usd + :cost, version = version + 1, updated_at = :now WHERE id = :id",
                {
                    "input": max(0, int(input_tokens)),
                    "output": max(0, int(output_tokens)),
                    "cost": max(0.0, float(cost_usd)),
                    "now": to_text(utcnow()),
                    "id": task_id,
                },
            )

    def attempt(self, task_id: str) -> int:
        with self.db.connection() as conn:
            row = conn.fetchone(
                "SELECT COUNT(*) AS retries FROM decisions WHERE operation = 'task_decision' "
                "AND object_id = :id AND decision = 'retry'",
                {"id": task_id},
            )
            task = conn.fetchone("SELECT rework_count FROM tasks WHERE id = :id", {"id": task_id})
        return row["retries"] + (task["rework_count"] if task else 0)

    def _attempt_events(self, conn: Connection, task_id: str, rework: bool = False) -> list[dict[str, Any]]:
        rows = conn.fetchall(
            "SELECT seq, type, occurred_at, payload FROM task_events WHERE task_id = :id "
            "AND type IN ('decision_recorded', 'decision_required', 'status_changed', 'rework_started') ORDER BY seq",
            {"id": task_id},
        )
        start = 0
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            retry = row["type"] == TaskEventType.decision_recorded and row["payload"].get("decision") == "retry"
            if retry or (rework and row["type"] == TaskEventType.rework_started):
                start = row["seq"]
        return [row for row in rows if row["seq"] > start]

    def published_version(self, task_id: str) -> int | None:
        with self.db.connection() as conn:
            task = self.load_task(conn, task_id)
            if task is None or task.spec is None:
                return None
            found = [
                row["payload"]["expected_version"]
                for row in self._attempt_events(conn, task_id)
                if row["type"] == TaskEventType.decision_required
                and row["payload"].get("decision") == "approve_spec"
            ]
        return found[-1] if found else None

    def attempt_approval(self, task_id: str) -> int | None:
        with self.db.connection() as conn:
            found = [
                row["payload"]["decision_id"]
                for row in self._attempt_events(conn, task_id)
                if row["type"] == TaskEventType.decision_recorded
                and row["payload"].get("decision") == "approve_spec"
            ]
            if not found:
                return None
            row = conn.fetchone("SELECT expected_version FROM decisions WHERE id = :id", {"id": found[-1]})
        return None if row is None else row["expected_version"]

    def attempt_started_at(self, task_id: str) -> datetime | None:
        with self.db.connection() as conn:
            for row in self._attempt_events(conn, task_id, rework=True):
                if row["type"] == TaskEventType.status_changed and row["payload"].get("to") == "developing":
                    return from_text(row["occurred_at"])
        return None

    def publish_spec(
        self, task_id: str, spec: dict[str, Any] | None, artifact: tuple[str, int] | None
    ) -> int | None:
        with self.db.locked_transaction() as conn:
            task = self.load_task(conn, task_id)
            if task is None or task.status != TaskStatus.awaiting_spec_approval:
                return None
            if spec is not None:
                conn.execute(
                    "UPDATE tasks SET spec = :spec, version = version + 1, updated_at = :now WHERE id = :id",
                    {"spec": _json(spec), "now": to_text(utcnow()), "id": task_id},
                )
            if artifact is not None:
                name, size = artifact
                self.record_event(
                    conn,
                    task_id,
                    TaskEventType.artifact_ready,
                    {"name": name, "size_bytes": size},
                    role=AgentRole.supervisor,
                    artifact=name,
                )
            version = self.load_task(conn, task_id).version
            self.record_event(
                conn,
                task_id,
                TaskEventType.decision_required,
                {"decision": "approve_spec", "expected_version": version},
            )
            return version

    def spec_approval(self, task_id: str, expected_version: int | None) -> str | None:
        if expected_version is None:
            return None
        with self.db.connection() as conn:
            task = self.load_task(conn, task_id)
            if task is None or task.status != TaskStatus.developing:
                return None
            row = conn.fetchone(
                "SELECT id FROM decisions WHERE operation = 'task_decision' AND object_id = :id "
                "AND decision = 'approve_spec' AND expected_version = :version ORDER BY recorded_at DESC",
                {"id": task_id, "version": expected_version},
            )
        return None if row is None else row["id"]

    def ensure_status(
        self, task_id: str, source: TaskStatus, target: TaskStatus, *, fix_cycles: int | None = None
    ) -> bool:
        with self.db.locked_transaction() as conn:
            task = self.load_task(conn, task_id)
            if task is None:
                return False
            if task.status == target and (fix_cycles is None or task.fix_cycles == fix_cycles):
                return True
            if task.status != source:
                return False
            if fix_cycles is not None:
                if fix_cycles != task.fix_cycles + 1:
                    return False
                conn.execute(
                    "UPDATE tasks SET fix_cycles = :cycles WHERE id = :id", {"cycles": fix_cycles, "id": task_id}
                )
            self.transition(conn, task_id, target, expected_from=source)
            return True

    def halt(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        statuses: frozenset[TaskStatus],
        role: AgentRole | None,
        step: str,
        reason: str,
        failure_kind: FailureKind | None = None,
    ) -> bool:
        with self.db.locked_transaction() as conn:
            return self.halt_in(
                conn, task_id, target, statuses=statuses, role=role, step=step, reason=reason,
                failure_kind=failure_kind,
            )

    def halt_in(
        self,
        conn: Connection,
        task_id: str,
        target: TaskStatus,
        *,
        statuses: frozenset[TaskStatus],
        role: AgentRole | None,
        step: str,
        reason: str,
        failure_kind: FailureKind | None = None,
    ) -> bool:
        task = self.load_task(conn, task_id)
        if task is None or task.status not in statuses:
            return False
        outcome = "blocked" if target == TaskStatus.needs_attention else "error"
        self.record_event(
            conn,
            task_id,
            TaskEventType.role_finished,
            {"step": step, "outcome": outcome, "summary": reason[:300]},
            role=role,
        )
        if failure_kind is not None:
            conn.execute(
                "UPDATE tasks SET failure_kind = :kind WHERE id = :id", {"kind": failure_kind.value, "id": task_id}
            )
        self.transition(conn, task_id, target, expected_from=task.status)
        return True

    def set_rework_reason(self, conn: Connection, task: Task, reason: str) -> None:
        text = reason if task.rework_count < self.rework_limit else f"{reason} {LIMIT_REACHED}"
        conn.execute("UPDATE tasks SET rework_reason = :reason WHERE id = :id", {"reason": text, "id": task.id})

    def fail_merged_in(self, conn: Connection, task_id: str, kind: FailureKind, reason: str) -> bool:
        task = self.load_task(conn, task_id)
        if task is None or (task.status == TaskStatus.failed and task.failure_kind == kind):
            return False
        movable = task.status in (TaskStatus.released, TaskStatus.failed) or TaskStatus.failed in TRANSITIONS[task.status]
        if not movable:
            return False
        self.record_event(
            conn, task_id, TaskEventType.role_finished, {"step": "release", "outcome": "error", "summary": reason[:300]}
        )
        conn.execute(
            "UPDATE tasks SET failure_kind = :kind, version = version + 1, updated_at = :now WHERE id = :id",
            {"kind": kind.value, "now": to_text(utcnow()), "id": task_id},
        )
        self.set_rework_reason(conn, task, reason)
        if task.status == TaskStatus.failed:
            return True
        if task.status != TaskStatus.released:
            self.transition(conn, task_id, TaskStatus.failed, expected_from=task.status)
            return True
        conn.execute(
            "UPDATE tasks SET status = :status, version = version + 1, updated_at = :now WHERE id = :id",
            {"status": TaskStatus.failed.value, "now": to_text(utcnow()), "id": task_id},
        )
        self.record_event(
            conn, task_id, TaskEventType.status_changed, {"from": task.status.value, "to": TaskStatus.failed.value}
        )
        return True

    def start_rework_in(self, conn: Connection, task_id: str, owner_id: str, context: dict[str, Any]) -> bool:
        task = self.load_task(conn, task_id)
        row = conn.fetchone("SELECT rework_of FROM tasks WHERE id = :id", {"id": task_id})
        if task is None or task.status != TaskStatus.failed or task.failure_kind not in REWORK_SOURCES:
            return False
        if row["rework_of"] == owner_id:
            return False
        reason = task.rework_reason or "нет данных"
        if task.rework_count >= self.rework_limit:
            conn.execute("UPDATE tasks SET rework_of = :owner WHERE id = :id", {"owner": owner_id, "id": task_id})
            self.record_event(
                conn, task_id, TaskEventType.role_finished,
                {"step": "rework", "outcome": "error", "summary": f"{LIMIT_REACHED} {reason}"[:300]},
            )
            return False
        attempt = task.rework_count + 1
        conn.execute(
            "UPDATE tasks SET rework_of = :owner, rework_count = :count, rework_priority = :yes, rework_context = :context, "
            "model_calls_limit = model_calls_limit + :calls WHERE id = :id",
            {
                "owner": owner_id, "count": attempt, "yes": True, "context": _json(context),
                "calls": MODEL_CALLS_LIMITS[task.change_class], "id": task_id,
            },
        )
        self.record_event(
            conn, task_id, TaskEventType.rework_started,
            {
                "reason": reason[:500], "attempt": attempt, "limit": self.rework_limit, "source": context["source"],
                "release_id": context["release_id"], "materials": list(REWORK_MATERIALS),
            },
        )
        self.transition(conn, task_id, TaskStatus.queued, expected_from=TaskStatus.failed)
        return True

    def rework_context(self, task_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.fetchone("SELECT rework_context FROM tasks WHERE id = :id", {"id": task_id})
        return json.loads(row["rework_context"]) if row and row["rework_context"] else None

    def save_rework_context(self, task_id: str, context: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET rework_context = :context WHERE id = :id", {"context": _json(context), "id": task_id}
            )

    def merge_task(self, conn: Connection, sha: str) -> str | None:
        row = conn.fetchone(
            "SELECT task_id FROM operations WHERE kind = 'merge_pr' AND status = 'succeeded' AND external_ref = :sha "
            "AND release_id IS NULL",
            {"sha": sha},
        )
        return row["task_id"] if row else None

    def _promote(self, conn: Connection) -> list[str]:
        promoted: list[str] = []
        used = self.slots_used(conn)
        while used < SLOTS_TOTAL:
            row = conn.fetchone(
                "SELECT id, rework_priority FROM tasks WHERE status = :queued "
                "ORDER BY rework_priority DESC, updated_at, id LIMIT 1",
                {"queued": TaskStatus.queued.value},
            )
            if row is None:
                break
            target = TaskStatus.awaiting_spec_approval
            if row["rework_priority"]:
                conn.execute(f"UPDATE tasks SET {REWORK_RESET} WHERE id = :id", {"no": False, "id": row["id"]})
                target = TaskStatus.developing
            self.transition(conn, row["id"], target, expected_from=TaskStatus.queued)
            promoted.append(row["id"])
            used += 1
        return promoted

    def promote_queue(self) -> list[str]:
        with self.db.locked_transaction() as conn:
            return self._promote(conn)

    def clear_reservations(self) -> None:
        with self.db.locked_transaction() as conn:
            conn.execute("DELETE FROM intake_reservations")

    def reserve_intake(self, participant_id: str, limit: int) -> str:
        now = utcnow()
        params = {"owner": participant_id, "since": to_text(now - timedelta(hours=1))}
        with self.db.locked_transaction() as conn:
            rows = conn.fetchall(
                "SELECT created_at FROM tasks WHERE owner_id = :owner AND created_at > :since "
                "UNION ALL "
                "SELECT created_at FROM intake_reservations WHERE participant_id = :owner AND created_at > :since",
                params,
            )
            if len(rows) >= limit:
                oldest = min(from_text(row["created_at"]) for row in rows)
                wait = math.ceil((oldest + timedelta(hours=1) - now).total_seconds())
                raise rate_limited("Можно поставить не больше пяти задач за час. Повторите позже.", wait)
            reservation_id = new_id("res")
            conn.execute(
                "INSERT INTO intake_reservations (id, participant_id, created_at) VALUES (:id, :owner, :now)",
                {"id": reservation_id, "owner": participant_id, "now": to_text(now)},
            )
        return reservation_id

    def drop_reservation(self, reservation_id: str) -> None:
        with self.db.locked_transaction() as conn:
            conn.execute("DELETE FROM intake_reservations WHERE id = :id", {"id": reservation_id})

    def create_task(
        self,
        owner_id: str,
        text: str,
        outcome: IntakeOutcome,
        reservation_id: str | None = None,
    ) -> Task:
        task_id = new_id("tsk")
        now = to_text(utcnow())
        title = task_title(text)
        status = TaskStatus.queued if outcome.accepted else TaskStatus.rejected
        with self.db.locked_transaction() as conn:
            if reservation_id is not None:
                conn.execute("DELETE FROM intake_reservations WHERE id = :id", {"id": reservation_id})
            conn.execute(
                "INSERT INTO tasks (id, owner_id, title, text, status, profile, version, created_at, updated_at, "
                "rejection_reason, spec, branch, base_sha, pr_url, changed_files, fix_cycles, model_calls, "
                "model_calls_limit, input_tokens, output_tokens, cost_usd, event_seq, change_class) VALUES (:id, "
                ":owner, :title, :text, :status, :profile, :version, :now, :now, :reason, NULL, NULL, NULL, NULL, "
                ":files, 0, :calls, :limit, :input_tokens, :output_tokens, :cost_usd, 0, :change_class)",
                {
                    "id": task_id,
                    "owner": owner_id,
                    "title": title,
                    "text": text,
                    "status": status.value,
                    "profile": LEGACY_PROFILE,
                    "version": 1 if outcome.accepted else 2,
                    "now": now,
                    "reason": None if outcome.accepted else outcome.reason,
                    "files": "[]",
                    "calls": outcome.model_calls,
                    "limit": MODEL_CALLS_LIMITS[outcome.change_class if outcome.accepted else ChangeClass.frontend_ui],
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                    "cost_usd": max(0.0, outcome.cost_usd),
                    "change_class": outcome.change_class.value,
                },
            )
            created: dict[str, Any] = {"title": title, "change_class": outcome.change_class.value}
            if outcome.model is not None:
                created |= {"provider": outcome.provider, "model": outcome.model, "effort": outcome.effort}
            self.record_event(conn, task_id, TaskEventType.task_created, created)
            if not outcome.accepted:
                self.record_event(conn, task_id, TaskEventType.task_rejected, {"reason": outcome.reason})
            task = self.load_task(conn, task_id)
        return task

    def events_after(self, task_id: str | None, after: int, limit: int = 100) -> list[TaskEvent]:
        params: dict[str, Any] = {"after": after, "limit": limit}
        where = "event_id > :after"
        if task_id is not None:
            where += " AND task_id = :task_id"
            params["task_id"] = task_id
        with self.db.connection() as conn:
            rows = conn.fetchall(
                f"SELECT * FROM task_events WHERE {where} ORDER BY event_id LIMIT :limit", params
            )
        return [event_from_row(row) for row in rows]

    def last_event_id(self) -> int:
        with self.db.connection() as conn:
            row = conn.fetchone("SELECT next_event_id FROM scheduler WHERE id = 1")
        return row["next_event_id"] - 1

    def latest_check_report(self, task: Task) -> CheckReport:
        with self.db.connection() as conn:
            row = conn.fetchone(
                "SELECT * FROM check_reports WHERE task_id = :id ORDER BY created_at DESC, id DESC LIMIT 1",
                {"id": task.id},
            )
        if row is None:
            return CheckReport(
                task_id=task.id, commit_sha=task.base_sha, spec_hash=None, suite_hash=None, checks=[]
            )
        return CheckReport(
            task_id=row["task_id"],
            commit_sha=row["commit_sha"],
            spec_hash=row["spec_hash"],
            suite_hash=row["suite_hash"],
            checks=json.loads(row["checks"]),
        )

    def check_reports(self, task_id: str, commit_sha: str) -> list[CheckReport]:
        with self.db.connection() as conn:
            rows = conn.fetchall(
                "SELECT * FROM check_reports WHERE task_id = :id AND commit_sha = :sha ORDER BY created_at DESC, id DESC",
                {"id": task_id, "sha": commit_sha},
            )
        return [
            CheckReport(
                task_id=row["task_id"],
                commit_sha=row["commit_sha"],
                spec_hash=row["spec_hash"],
                suite_hash=row["suite_hash"],
                checks=json.loads(row["checks"]),
            )
            for row in rows
        ]

    def add_check_report(self, report: CheckReport) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO check_reports (id, task_id, created_at, commit_sha, spec_hash, suite_hash, checks) "
                "VALUES (:id, :task_id, :now, :commit_sha, :spec_hash, :suite_hash, :checks)",
                {
                    "id": new_id("chk"),
                    "task_id": report.task_id,
                    "now": to_text(utcnow()),
                    "commit_sha": report.commit_sha,
                    "spec_hash": report.spec_hash,
                    "suite_hash": report.suite_hash,
                    "checks": _json([check.model_dump() for check in report.checks]),
                },
            )

    def load_candidate(self, conn: Connection, release_id: str) -> ReleaseCandidate | None:
        row = conn.fetchone("SELECT * FROM release_candidates WHERE id = :id", {"id": release_id})
        return None if row is None else candidate_from_row(row)

    def list_releases(self) -> list[ReleaseCandidate]:
        with self.db.connection() as conn:
            rows = conn.fetchall("SELECT * FROM release_candidates ORDER BY created_at DESC, id DESC")
        return [candidate_from_row(row) for row in rows]

    def get_candidate(self, release_id: str) -> ReleaseCandidate:
        with self.db.connection() as conn:
            candidate = self.load_candidate(conn, release_id)
        if candidate is None:
            raise not_found("Кандидат выпуска не найден.")
        return candidate

    def update_candidate(self, conn: Connection, release_id: str, **fields: Any) -> None:
        values = {
            name: _json(value.model_dump(mode="json")) if isinstance(value, BaseModel) else value
            for name, value in fields.items()
        }
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        conn.execute(
            f"UPDATE release_candidates SET {assignments}, version = version + 1 WHERE id = :release_id",
            values | {"release_id": release_id},
        )

    def queue_state(self, conn: Connection) -> dict[str, Any]:
        return conn.fetchone(
            "SELECT active_release_id, release_blocked_by, published_sha, external_wait_since FROM scheduler "
            "WHERE id = 1"
        )

    def set_queue(self, conn: Connection, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        conn.execute(f"UPDATE scheduler SET {assignments} WHERE id = 1", fields)

    def create_candidate(
        self,
        conn: Connection,
        *,
        task_id: str,
        kind: ReleaseKind,
        commit_sha: str,
        expected_previous_sha: str,
        spec_hash: str,
        suite_hash: str,
        ci_run_ids: list[str],
        pr_url: str | None,
        status: ReleaseStatus = ReleaseStatus.pending,
        is_published: bool = False,
        pr_review_id: str | None = None,
        e2e: CheckReport | None = None,
        origin: ReleaseOrigin = ReleaseOrigin.task,
        origin_decision_id: str | None = None,
        reason: str | None = None,
    ) -> ReleaseCandidate:
        release_id = new_id("rel")
        conn.execute(
            "INSERT INTO release_candidates (id, task_id, kind, status, version, commit_sha, expected_previous_sha, "
            "spec_hash, suite_hash, ci_run_ids, pr_url, created_at, approved_by, approved_at, is_published, deploy, "
            "pr_review_id, e2e, origin, origin_decision_id, reason) "
            "VALUES (:id, :task_id, :kind, :status, 1, :commit_sha, :previous, :spec_hash, :suite_hash, :ci, "
            ":pr_url, :now, NULL, NULL, :published, NULL, :review, :e2e, :origin, :decision, :reason)",
            {
                "id": release_id,
                "task_id": task_id,
                "kind": kind.value,
                "status": status.value,
                "commit_sha": commit_sha,
                "previous": expected_previous_sha,
                "spec_hash": spec_hash,
                "suite_hash": suite_hash,
                "ci": _json(ci_run_ids),
                "pr_url": pr_url,
                "now": to_text(utcnow()),
                "published": is_published,
                "review": pr_review_id,
                "e2e": _json(e2e.model_dump(mode="json")) if e2e is not None else None,
                "origin": origin.value,
                "decision": origin_decision_id,
                "reason": reason,
            },
        )
        self.record_event(
            conn,
            task_id,
            TaskEventType.release_candidate,
            {"release_id": release_id, "commit_sha": commit_sha},
        )
        return self.load_candidate(conn, release_id)
