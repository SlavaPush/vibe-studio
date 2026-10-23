import json
import logging
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Any

from app.db import Connection, Database, from_text, to_text, utcnow
from app.errors import not_found, rate_limited
from app.schemas import (
    SLOT_STATUSES,
    SLOTS_TOTAL,
    AgentRole,
    CheckReport,
    ModelProfile,
    ReleaseCandidate,
    ReleaseKind,
    ReleaseStatus,
    Task,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)

logger = logging.getLogger("office.tasks")

MODEL_CALLS_LIMIT = 30
SIGNAL_SLOT_ACQUIRED = "slot_acquired"
SIGNAL_DECISION_RECORDED = "decision_recorded"
SIGNAL_TASK_CLOSED = "task_closed"
CLOSED_STATUSES = frozenset({TaskStatus.cancelled, TaskStatus.released})

S = TaskStatus
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.rejected: frozenset(),
    S.queued: frozenset({S.awaiting_spec_approval, S.cancelled}),
    S.awaiting_spec_approval: frozenset({S.developing, S.cancelled, S.failed, S.needs_attention}),
    S.developing: frozenset({S.reviewing, S.cancelled, S.failed, S.needs_attention}),
    S.reviewing: frozenset(
        {S.developing, S.awaiting_integration, S.cancelled, S.failed, S.needs_attention}
    ),
    S.awaiting_integration: frozenset(
        {S.awaiting_release_approval, S.cancelled, S.failed, S.needs_attention}
    ),
    S.awaiting_release_approval: frozenset({S.deploying, S.failed, S.needs_attention}),
    S.deploying: frozenset({S.released, S.failed, S.needs_attention}),
    S.released: frozenset(),
    S.failed: frozenset({S.queued}),
    S.cancelled: frozenset(),
    S.needs_attention: frozenset({S.queued, S.cancelled}),
}

TASK_SELECT = (
    "SELECT t.*, p.display_name AS owner_name FROM tasks t "
    "JOIN participants p ON p.id = t.owner_id"
)

SignalHandler = Callable[[str, str], None]


class TransitionError(Exception):
    pass


@dataclass(frozen=True)
class IntakeOutcome:
    accepted: bool
    reason: str | None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def task_title(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip()[:80] if lines else ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def task_from_row(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        text=row["text"],
        status=row["status"],
        profile=row["profile"],
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
        changed_files=json.loads(row["changed_files"]),
        fix_cycles=row["fix_cycles"],
        usage={
            "model_calls": row["model_calls"],
            "model_calls_limit": row["model_calls_limit"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        },
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
        approved_by=row["approved_by"],
        approved_at=from_text(row["approved_at"]) if row["approved_at"] else None,
        is_published=bool(row["is_published"]),
        deploy=json.loads(row["deploy"]) if row["deploy"] else None,
    )


class TaskStore:
    def __init__(self, db: Database, notify: Callable[[], None] = lambda: None) -> None:
        self.db = db
        self.notify = notify
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
        return None if row is None else task_from_row(row)

    def get_task(self, task_id: str) -> Task:
        with self.db.connection() as conn:
            task = self.load_task(conn, task_id)
        if task is None:
            raise not_found("Задача не найдена.")
        return task

    def list_tasks(self) -> list[Task]:
        with self.db.connection() as conn:
            rows = conn.fetchall(f"{TASK_SELECT} ORDER BY t.created_at DESC, t.id DESC")
        return [task_from_row(row) for row in rows]

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
            if task is None or task.status not in SLOT_STATUSES:
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

    def record_commit(self, task_id: str, sha: str, changed_files: list[str]) -> Task:
        with self.db.locked_transaction() as conn:
            conn.execute(
                "UPDATE tasks SET changed_files = :files, version = version + 1, updated_at = :now WHERE id = :id",
                {"files": _json(changed_files), "now": to_text(utcnow()), "id": task_id},
            )
            self.record_event(
                conn,
                task_id,
                TaskEventType.tool_called,
                {"arguments": f"{sha} ({len(changed_files)} файлов)", "result": "ok"},
                tool="git_commit",
            )
            return self.load_task(conn, task_id)

    def _promote(self, conn: Connection) -> list[str]:
        promoted: list[str] = []
        used = self.slots_used(conn)
        while used < SLOTS_TOTAL:
            row = conn.fetchone(
                "SELECT id FROM tasks WHERE status = :queued ORDER BY updated_at, id LIMIT 1",
                {"queued": TaskStatus.queued.value},
            )
            if row is None:
                break
            self.transition(
                conn, row["id"], TaskStatus.awaiting_spec_approval, expected_from=TaskStatus.queued
            )
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
        profile: ModelProfile,
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
                "model_calls_limit, input_tokens, output_tokens, event_seq) VALUES (:id, :owner, :title, :text, "
                ":status, :profile, :version, :now, :now, :reason, NULL, NULL, NULL, NULL, :files, 0, :calls, "
                ":limit, :input_tokens, :output_tokens, 0)",
                {
                    "id": task_id,
                    "owner": owner_id,
                    "title": title,
                    "text": text,
                    "status": status.value,
                    "profile": profile.value,
                    "version": 1 if outcome.accepted else 2,
                    "now": now,
                    "reason": None if outcome.accepted else outcome.reason,
                    "files": "[]",
                    "calls": outcome.model_calls,
                    "limit": MODEL_CALLS_LIMIT,
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                },
            )
            self.record_event(
                conn, task_id, TaskEventType.task_created, {"title": title, "profile": profile.value}
            )
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
    ) -> ReleaseCandidate:
        release_id = new_id("rel")
        conn.execute(
            "INSERT INTO release_candidates (id, task_id, kind, status, version, commit_sha, expected_previous_sha, "
            "spec_hash, suite_hash, ci_run_ids, pr_url, created_at, approved_by, approved_at, is_published, deploy) "
            "VALUES (:id, :task_id, :kind, :status, 1, :commit_sha, :previous, :spec_hash, :suite_hash, :ci, "
            ":pr_url, :now, NULL, NULL, :published, NULL)",
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
            },
        )
        self.record_event(
            conn,
            task_id,
            TaskEventType.release_candidate,
            {"release_id": release_id, "commit_sha": commit_sha},
        )
        return self.load_candidate(conn, release_id)
