import json
import logging
import threading
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from app.branch_check import PACKAGE_JSON, PYPROJECT, SCHEMA_SQL, NPM_SECTIONS, WorkspaceError
from app.config import APP_DIR, Settings
from app.db import Connection, from_text, to_text, utcnow
from app.github import ACTIONS_APP_ID, GitHubClient, GitHubError, GitHubUnavailable
from app.models_gateway import InvalidModelResponse, ModelsGateway, ProviderError
from app.operations import Operation, OperationLog, OpStatus
from app.release_queue import QueueBusy, ReleaseQueue, fixed_targets, targets_of
from app.render import RenderClient, RenderError, RenderUnavailable
from app.required_checks import RequiredCheck
from app.roles import CHECKLIST, PR_REVIEWER, ContextTooLarge, Entry, PrReview, assemble
from app.sandbox import CommandsPolicy, SandboxCancelled, SandboxError, suite_commands, suite_steps
from app.schemas import (
    AgentRole,
    ChangeClass,
    CheckReport,
    Contour,
    Decision,
    FailureKind,
    OwnerReview,
    Participant,
    PullRequestCheck,
    PullRequestChecks,
    PullRequestReview,
    ReleaseCandidate,
    ReleaseDecisionRequest,
    ReleaseKind,
    ReleaseOrigin,
    ReleaseStatus,
    Task,
    TaskEventType,
    TaskStatus,
)
from app.service_check import SERVICE_TIMEOUT, BaseUnavailable, ServiceCheck
from app.tasks import (
    SIGNAL_DECISION_RECORDED,
    SIGNAL_SLOT_RELEASED,
    SIGNAL_TASK_CLOSED,
    BudgetExceeded,
    TaskStore,
    _json,
    new_id,
    rework_tree,
)
from app.workspace import NETWORK_TIMEOUT, PushResult, Workspace, branch_name

logger = logging.getLogger("office.release")

S = TaskStatus
PUSH = "push"
CREATE_PR = "create_pr"
CLOSE_PR = "close_pr"
PR_REVIEW = "pr_review"
MERGE_PR = "merge_pr"
REVERT = "revert"
OWNER_SUMMARY = "owner_summary"
DEPENDENCY_FILES = (PACKAGE_JSON, f"{APP_DIR}/web/package-lock.json", PYPROJECT, f"{APP_DIR}/api/uv.lock")
SCHEMA_ONLY = (
    "изменение затрагивает только схему БД; проверьте текущий main и снимите блокировку решением "
    "resume_releases с fix_sha"
)
INTEGRATION_OPS = frozenset({PUSH, CREATE_PR, CLOSE_PR, PR_REVIEW, MERGE_PR})
MAX_ATTEMPTS = 3
STEPS_PER_PASS = 8
SHUTDOWN_TIMEOUT = 30.0
REVIEW_DIFF_LIMIT = 200 * 1024
INTEGRATING = frozenset({S.awaiting_integration})
CANDIDATES = (
    "SELECT id, created_at FROM tasks WHERE status = 'awaiting_integration' "
    "UNION SELECT t.id, t.created_at FROM tasks t WHERE t.status = 'cancelled' "
    "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.type = 'operation_unknown') "
    "AND ((t.pr_number IS NOT NULL AND t.merge_sha IS NULL AND NOT EXISTS (SELECT 1 FROM operations o "
    "WHERE o.task_id = t.id AND o.kind = 'close_pr' AND o.status = 'succeeded')) "
    "OR EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id AND o.release_id IS NULL "
    "AND o.kind IN ('create_pr', 'close_pr', 'merge_pr') AND o.status IN ('started', 'unknown'))) "
    "ORDER BY created_at, id"
)


@dataclass(frozen=True)
class Carrier:
    id: str
    task_id: str
    tree: str
    title: str
    base_sha: str | None
    head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    merge_sha: str | None
    checks: PullRequestChecks | None
    release_id: str | None = None
    change_class: ChangeClass | None = None
    owner_review: OwnerReview | None = None

    @property
    def branch(self) -> str:
        return branch_name(self.tree)

    @property
    def awaiting_owner(self) -> bool:
        return (
            not self.merge_sha and self.owner_review is not None and self.owner_review.state == "requested"
            and self.owner_review.head_sha == self.head_sha
        )


def task_carrier(task: Task) -> Carrier:
    tree = rework_tree(task.id, task.rework_count)
    return Carrier(
        id=tree, task_id=task.id, tree=tree, title=task.title, base_sha=task.base_sha,
        head_sha=task.head_sha, pr_number=task.pr_number, pr_url=task.pr_url, merge_sha=task.merge_sha,
        checks=task.checks, change_class=task.change_class, owner_review=task.owner_review,
    )


def revert_id(release_id: str, attempt: int) -> str:
    return f"{REVERT}:{release_id}" if attempt == 1 else f"{REVERT}:{release_id}:{attempt}"


def revert_carrier(release: ReleaseCandidate, task: Task) -> Carrier:
    revert = release.revert
    return Carrier(
        id=revert_id(release.id, revert.attempt), task_id=release.task_id, tree=revert.tree or f"revert-{release.id}",
        title=f"Revert: {task.title}"[:120], base_sha=revert.base_sha, head_sha=revert.head_sha,
        pr_number=revert.pr_number, pr_url=revert.pr_url, merge_sha=revert.fix_sha, checks=revert.checks,
        release_id=release.id, owner_review=revert.owner_review,
    )


class ReleaseWorker:
    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        workspace: Workspace,
        github: GitHubClient,
        required: list[RequiredCheck],
        clock: Callable[[], datetime] = utcnow,
        *,
        gateway: ModelsGateway,
        suite: Any,
        service: httpx.Client | None = None,
        render: RenderClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace = workspace
        self.github = github
        self.required = required
        self.clock = clock
        self.gateway = gateway
        self.suite = suite
        self.service = service or httpx.Client(timeout=SERVICE_TIMEOUT)
        self.commands = CommandsPolicy(settings.commands_policy)
        self.ops = OperationLog(store.db)
        self._wake = threading.Condition()
        self._pending = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.queue = ReleaseQueue(
            settings, store, github, render, ServiceCheck(self.service, settings.service_url), clock, self.ops,
            self._pause,
        )
        store.signal_handlers.append(self.on_signal)

    def on_signal(self, name: str, task_id: str) -> None:
        if name in (SIGNAL_SLOT_RELEASED, SIGNAL_DECISION_RECORDED, SIGNAL_TASK_CLOSED):
            self.wake()

    def wake(self) -> None:
        with self._wake:
            self._pending = True
            self._wake.notify_all()

    def _pause(self, seconds: float) -> bool:
        with self._wake:
            self._wake.wait_for(lambda: self._stopping, seconds)
            return not self._stopping

    def start(self) -> None:
        with self._wake:
            if self._stopping:
                return
            self._thread = threading.Thread(target=self._loop, name="release", daemon=True)
            self._thread.start()

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        if self.on_signal in self.store.signal_handlers:
            self.store.signal_handlers.remove(self.on_signal)
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.warning("release thread is still running at shutdown")

    def _loop(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                self._pending = False
            try:
                self.run_once()
            except Exception:
                logger.exception("release pass failed")
            with self._wake:
                self._wake.wait_for(lambda: self._pending or self._stopping, self.settings.release_poll_seconds)

    def candidates(self) -> list[str]:
        with self.store.db.connection() as conn:
            return [row["id"] for row in conn.fetchall(CANDIDATES)]

    def run_once(self) -> None:
        for task_id in self.candidates():
            self._drive(lambda: self.step(task_id), task_id)
        self._drive(self.revert_step, "revert")
        self._drive(self.queue.step, "queue")

    def _drive(self, step: Callable[[], bool], name: str) -> None:
        for _ in range(STEPS_PER_PASS):
            if self._stopping:
                return
            try:
                if not step():
                    return
            except (GitHubError, GitHubUnavailable, RenderError, RenderUnavailable) as error:
                logger.warning("release step of %s stopped: %s", name, error)
                return
            except Exception:
                logger.exception("release step failed: %s", name)
                return

    def decide(self, participant: Participant, release_id: str, request: ReleaseDecisionRequest) -> Decision:
        return self.queue.decide(participant, release_id, request)

    def step(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        if task.status not in (S.awaiting_integration, S.cancelled):
            return False
        c = task_carrier(task)
        pending = self.ops.open_for(task_id)
        if task.status == S.cancelled:
            pending = [operation for operation in pending if operation.kind not in (PUSH, PR_REVIEW)]
        if pending:
            return self._reconcile(c, pending[0])
        if task.status == S.cancelled:
            closed = self.ops.find(f"{CLOSE_PR}:{c.id}")
            if task.merge_sha or task.pr_number is None or (closed is not None and closed.status == OpStatus.succeeded):
                return False
            return self._close(c)
        return self._integrate(c)

    def revert_step(self) -> bool:
        with self.store.db.connection() as conn:
            blocked = self.store.queue_state(conn)["release_blocked_by"]
            release = self.store.load_candidate(conn, blocked) if blocked else None
        if release is None or release.revert is None:
            return False
        if self._needs_attempt(release):
            return self._next_attempt(release)
        if release.revert.status not in ("pending", "active"):
            return False
        c = revert_carrier(release, self.store.get_task(release.task_id))
        if release.revert.status == "pending":
            return self._start_revert(c, release)
        pending = [op for op in self.ops.open_for(c.task_id, c.release_id) if op.kind in INTEGRATION_OPS]
        if pending and release.revert.source == "auto":
            return self._reconcile(c, pending[0])
        return self._integrate(c)

    def _needs_attempt(self, release: ReleaseCandidate) -> bool:
        revert = release.revert
        statuses = ("active", "merged", "needs_attention") if revert.source == "auto" else ("merged",)
        if revert.status not in statuses:
            return False
        added = [target for target in revert.targets if target.fixed_by is None and target.attempt != revert.attempt]
        return bool(added) and not self.ops.open_for(release.task_id, release.id)

    def _next_attempt(self, release: ReleaseCandidate) -> bool:
        with self.store.db.locked_transaction() as conn:
            current = self.store.load_candidate(conn, release.id)
            if self.store.queue_state(conn)["release_blocked_by"] != release.id or current.version != release.version:
                return False
            revert = current.revert.model_copy(
                update={
                    "status": "pending", "source": "auto", "attempt": current.revert.attempt + 1, "fix_sha": None,
                    "candidate_id": None, "reason": None,
                }
            )
            self.store.update_candidate(conn, release.id, revert=revert)
            self.store.record_event(
                conn, release.task_id, TaskEventType.role_finished,
                {
                    "step": REVERT, "outcome": "ok", "revert_of": release.id,
                    "summary": f"В набор отмены добавлены новые слияния: revert, попытка {revert.attempt}.",
                },
            )
        return True

    def _fail_targets(self, found: list[tuple[Any, list[str]]]) -> None:
        with self.store.db.locked_transaction() as conn:
            for target, shas in found:
                release = self.store.load_candidate(conn, target.release_id)
                reason = (release.blocked_reason if release else None) or "Изменение отменено revert."
                for sha in shas:
                    task_id = self.store.merge_task(conn, sha)
                    if task_id is not None:
                        self.store.fail_merged_in(conn, task_id, FailureKind(target.source), reason)

    def _start_revert(self, c: Carrier, release: ReleaseCandidate) -> bool:
        if release.revert.source == "resume":
            fix = release.revert.fix_sha
            covered = [
                target for target in release.revert.targets
                if target.fixed_by is None and target.attempt == release.revert.attempt and target.release_id != release.id
            ]
            try:
                in_main = self.github.compare(fix, "main")
                after = self.github.compare(release.commit_sha, fix)
                outside = {
                    target.release_id for target in covered
                    if self.github.compare(target.head_sha, fix).get("status") != "ahead"
                }
            except GitHubError as error:
                if error.status in (404, 422):
                    return self._halt(c, f"Исправляющий commit {fix[:7]} не найден в репозитории.", step=REVERT)
                logger.warning("fix commit of %s is not checked: %s", release.id, error)
                return False
            except GitHubUnavailable as error:
                logger.warning("fix commit of %s is not checked: %s", release.id, error)
                return False
            if in_main.get("status") not in ("ahead", "identical") or in_main.get("behind_by") != 0:
                return self._halt(c, f"Исправляющий commit {fix[:7]} не входит в историю main.", step=REVERT)
            if after.get("status") != "ahead":
                return self._halt(
                    c, f"Исправляющий commit {fix[:7]} не является потомком откатанного {release.commit_sha[:7]}.",
                    step=REVERT,
                )
            targets = [
                target.model_copy(update={"attempt": None}) if target.release_id in outside else target
                for target in release.revert.targets
            ]
            with self.store.db.locked_transaction() as conn:
                self._save_in(conn, c, status="active", targets=targets)
                self._event(
                    conn, c, TaskEventType.role_finished,
                    {"step": REVERT, "outcome": "ok", "summary": f"Исправляющий commit {fix[:7]} принят: проверяется итоговый commit."},
                )
            return True
        tree = c.tree
        if release.revert.attempt > 1:
            if not self._close_previous(c, release):
                return False
            if self.github.branch_sha(c.branch) is not None:
                tree = f"revert-{release.id}-{release.revert.attempt}"
                with suppress(WorkspaceError):
                    self.workspace.remove_worktree(c.tree)
        found = self.ops.find(c.id)
        if found is not None and found.status == OpStatus.started:
            self.ops.mark(found, OpStatus.failed, detail="restart")
        operation, allowed = self.ops.begin(REVERT, c.id, c.task_id, release.id)
        if not allowed:
            return False
        targets = targets_of(release)
        done = {sha for target in targets if target.fixed_by for sha in target.merges or []}
        pending = [target for target in targets if target.fixed_by is None]
        ranges = ", ".join(f"{target.base_sha[:7]}..{target.head_sha[:7]}" for target in pending)
        found: list[tuple[Any, list[str]]] = []

        def covered() -> list[Any]:
            merges = {target.release_id: shas for target, shas in found}
            return [
                target.model_copy(
                    update={"merges": merges.get(target.release_id, target.merges), "attempt": release.revert.attempt}
                )
                if target.fixed_by is None else target
                for target in targets
            ]

        try:
            picked: dict[str, list[str]] = {}
            for target in pending:
                line = self.workspace.first_parent(target.base_sha, target.head_sha)
                merges = [item for item in line if item[0] not in done and self.queue.office_merge(item[0])]
                found.append((target, [item[0] for item in merges]))
                for item in merges:
                    picked.setdefault(item[0], item)
            if not picked:
                with self.store.db.locked_transaction() as conn:
                    self.ops.finish(conn, operation, OpStatus.failed, detail="no office merges")
                    self._save_in(conn, c, targets=covered())
                    self._halt_in(
                        conn, c,
                        f"В диапазоне {ranges} нет слияний PR офиса: revert не создаётся. "
                        "Исправьте main и укажите fix_sha в решении «Возобновить выпуск».",
                        step=REVERT,
                    )
                return False
            order = {sha: index for index, sha in enumerate(self.workspace.main_line())}
            commits = sorted(picked.values(), key=lambda item: order.get(item[0], len(order)))
            self._fail_targets(found)
            base, head = self.workspace.revert_branch(tree, commits)
            empty = not self.workspace.changed_names(tree, base, head)
        except WorkspaceError as error:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=str(error)[:300])
                self._save_in(conn, c, targets=covered())
                self._halt_in(conn, c, f"Revert откатанного выпуска не создан: {error}", step=REVERT)
            return False
        if empty:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail="schema only")
                self._save_in(conn, c, targets=covered())
                self._halt_in(conn, c, SCHEMA_ONLY, step=REVERT)
            return False
        updated = covered()
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=head)
            self._save_in(
                conn, c, status="active", base_sha=base, head_sha=None, pr_number=None, pr_url=None, checks=None,
                fix_sha=None, candidate_id=None, tree=None if tree == f"revert-{release.id}" else tree, targets=updated,
            )
            self._event(
                conn, c, TaskEventType.tool_called,
                {"arguments": f"{branch_name(tree)} {' '.join(sha[:7] for sha, *_ in commits)}", "result": "ok"},
                tool="git_revert",
            )
        return True

    def _close_previous(self, c: Carrier, release: ReleaseCandidate) -> bool:
        with self.store.db.connection() as conn:
            rows = conn.fetchall(
                "SELECT payload FROM task_events WHERE task_id = :id AND type = 'pr_opened' ORDER BY seq",
                {"id": c.task_id},
            )
        opened = (json.loads(row["payload"]) for row in rows)
        numbers = dict.fromkeys(payload["pr_number"] for payload in opened if payload.get("revert_of") == release.id)
        return all(self._close_pr_of(c, release, number) for number in numbers)

    def _close_pr_of(self, c: Carrier, release: ReleaseCandidate, number: int) -> bool:
        is_open = self.github.get_pr(number).get("state") == "open"
        key = f"{CLOSE_PR}:{c.id}:{number}"
        found = self.ops.find(key)
        if found is not None and found.status == OpStatus.started:
            self.ops.mark(found, OpStatus.failed if is_open else OpStatus.succeeded, detail="restart")
        if not is_open:
            return True
        operation, allowed = self.ops.begin(CLOSE_PR, key, c.task_id, release.id)
        if not allowed:
            return self._halt(c, f"Прежний revert-PR #{number} открыт вне офиса после закрытия.", step=CLOSE_PR)
        try:
            self.github.close_pr(number)
        except GitHubUnavailable:
            self.ops.mark(operation, OpStatus.failed, detail="unavailable")
            return False
        except GitHubError as error:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {error.status}")
                self._halt_in(conn, c, f"GitHub отклонил закрытие прежнего revert-PR #{number}: HTTP {error.status}.", step=CLOSE_PR)
            return False
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(number))
            self._event(conn, c, TaskEventType.pr_closed, {"pr_number": number})
        return True

    def _status(self, task_id: str) -> TaskStatus:
        return self.store.get_task(task_id).status

    def _active(self, c: Carrier) -> bool:
        with self.store.db.connection() as conn:
            return self._active_in(conn, c)

    def _active_in(self, conn: Connection, c: Carrier) -> bool:
        if c.release_id is None:
            return self.store.load_task(conn, c.task_id).status == S.awaiting_integration
        release = self.store.load_candidate(conn, c.release_id)
        return (
            self.store.queue_state(conn)["release_blocked_by"] == c.release_id
            and release.revert is not None
            and release.revert.status == "active"
        )

    def _carrier_in(self, conn: Connection, c: Carrier) -> Carrier:
        task = self.store.load_task(conn, c.task_id)
        if c.release_id is None:
            return task_carrier(task)
        return revert_carrier(self.store.load_candidate(conn, c.release_id), task)

    def _reload(self, c: Carrier) -> Carrier:
        with self.store.db.connection() as conn:
            return self._carrier_in(conn, c)

    def _save_in(self, conn: Connection, c: Carrier, **fields: Any) -> None:
        if c.release_id is not None:
            release = self.store.load_candidate(conn, c.release_id)
            update = {("fix_sha" if name == "merge_sha" else name): value for name, value in fields.items()}
            self.store.update_candidate(conn, c.release_id, revert=release.revert.model_copy(update=update))
            return
        values = {name: _json(value.model_dump()) if isinstance(value, BaseModel) else value for name, value in fields.items()}
        assignments = "".join(f"{name} = :{name}, " for name in values)
        conn.execute(
            f"UPDATE tasks SET {assignments}version = version + 1, updated_at = :now WHERE id = :id",
            values | {"now": to_text(utcnow()), "id": c.task_id},
        )

    def _event(self, conn: Connection, c: Carrier, event_type: TaskEventType, payload: dict[str, Any], **kwargs: Any) -> None:
        if c.release_id is not None:
            payload = payload | {"revert_of": c.release_id}
        self.store.record_event(conn, c.task_id, event_type, payload, **kwargs)

    def _halt_in(
        self, conn: Connection, c: Carrier, reason: str, *, step: str = "release", role: AgentRole | None = None,
        failure_kind: FailureKind | None = None,
    ) -> None:
        if c.release_id is None:
            self.store.halt_in(
                conn, c.task_id, S.failed if failure_kind else S.needs_attention, statuses=INTEGRATING, role=role,
                step=step, reason=reason, failure_kind=failure_kind,
            )
            return
        release = self.store.load_candidate(conn, c.release_id)
        if release.revert is None or release.revert.status not in ("pending", "active"):
            return
        self._save_in(conn, c, status="needs_attention", reason=reason)
        self._event(
            conn, c, TaskEventType.role_finished,
            {"step": step, "outcome": "blocked", "summary": f"Исправление после отката остановлено: {reason}"[:300]},
            role=role,
        )

    def _halt(
        self, c: Carrier, reason: str, *, step: str = "release", role: AgentRole | None = None,
        failure_kind: FailureKind | None = None,
    ) -> bool:
        with self.store.db.locked_transaction() as conn:
            self._halt_in(conn, c, reason, step=step, role=role, failure_kind=failure_kind)
        return False

    def _integrate(self, c: Carrier) -> bool:
        if c.merge_sha:
            return self._finish(c)
        try:
            head = self.workspace.head(c.tree)
        except WorkspaceError as error:
            return self._halt(c, f"Рабочая копия задачи недоступна: {error}")
        if c.head_sha != head:
            return self._push(c, head)
        if c.pr_number is None:
            return self._open_pr(c)
        return self._check(c)

    def _push(self, c: Carrier, head: str) -> bool:
        try:
            found = self.workspace.verify_branch(c.tree, c.base_sha, c.change_class)
        except WorkspaceError as error:
            found = [str(error)]
        if found:
            return self._halt(c, f"Повторная проверка ветки перед push не пройдена: {'; '.join(found)}")
        token = self.github.auth.token()
        if not self._active(c):
            return False
        operation, allowed = self.ops.begin(PUSH, f"{PUSH}:{c.id}:{head}", c.task_id, c.release_id)
        if not allowed:
            if operation.status == OpStatus.succeeded:
                self._apply_push(operation, c, head)
                return True
            return False
        result = self.workspace.push(c.tree, self.settings.push_url, token, head)
        if result == PushResult.ok:
            self._apply_push(operation, c, head)
            return True
        if result == PushResult.rejected:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail="rejected")
                self._halt_in(
                    conn, c,
                    f"GitHub отклонил push ветки {c.branch}: ветка изменена вне офиса или push запрещён.",
                    step="git_push",
                )
            return False
        if operation.attempt >= MAX_ATTEMPTS:
            self._unknown(c, operation, f"Push ветки {c.branch} не выполнен за {MAX_ATTEMPTS} попытки.")
        return False

    def _open_pr(self, c: Carrier) -> bool:
        found = self.github.pulls(c.branch, "open")
        if found:
            self._apply_pr(None, c, found[0])
            return True
        if not self._active(c):
            return False
        body = self._body(c)
        operation, allowed = self.ops.begin(CREATE_PR, f"{CREATE_PR}:{c.id}", c.task_id, c.release_id)
        if not allowed:
            return False
        pr = None
        status = None
        try:
            pr = self.github.create_pr(c.branch, c.title, body)
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        if pr is not None:
            self._apply_pr(operation, c, pr)
            return True
        if status is not None and status < 500:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {status}")
                self._halt_in(conn, c, f"GitHub отклонил создание PR: HTTP {status}.", step="create_pr")
            return False
        if operation.attempt >= MAX_ATTEMPTS:
            self._unknown(c, operation, f"PR не создан за {MAX_ATTEMPTS} попытки.")
        return False

    def _check(self, c: Carrier) -> bool:
        minutes = f"{self.settings.checks_timeout_minutes:g}"
        try:
            pr = self.github.get_pr(c.pr_number)
            head = (pr.get("head") or {}).get("sha")
            if pr.get("state") != "open":
                return self._halt(c, f"PR #{c.pr_number} закрыт вне офиса.")
            if head != c.head_sha:
                if self.github.branch_sha(c.branch) != c.head_sha:
                    return self._halt(
                        c, f"Ветка PR #{c.pr_number} изменена вне офиса: head {str(head)[:7]} вместо "
                        f"{c.head_sha[:7]}."
                    )
                if self._checks_expired(c):
                    return self._halt(c, f"Head PR #{c.pr_number} не обновился за {minutes} мин.")
                return False
            summary = self.summary(c)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("checks of %s are not read: %s", c.id, error)
            if self._checks_expired(c):
                return self._halt(c, f"Обязательные проверки PR не прочитаны за {minutes} мин: {error}.")
            return False
        if c.checks != summary:
            with self.store.db.locked_transaction() as conn:
                self._save_in(conn, c, checks=summary)
                self._event(conn, c, TaskEventType.checks_status, summary.model_dump())
        failed = [check for check in summary.checks if check.status == "completed" and check.conclusion != "success"]
        if failed:
            return self._close(self._reload(c))
        waiting = [check.name for check in summary.checks if check.status != "completed"]
        if waiting:
            if self._checks_expired(c):
                return self._halt(
                    c, f"Обязательные проверки PR не завершились за {minutes} мин: {', '.join(waiting)}."
                )
            return False
        return self._review(self._reload(c))

    def _review(self, c: Carrier) -> bool:
        try:
            found = self.workspace.verify_branch(c.tree, c.base_sha, c.change_class)
            contour = self._contour(c)
        except WorkspaceError as error:
            found = [str(error)]
        if found:
            return self._halt(c, f"Повторная проверка ветки перед ревью PR не пройдена: {'; '.join(found)}")
        with self.store.db.connection() as conn:
            review = self.store.load_review(conn, c.task_id, c.head_sha)
        if review is None:
            return self._ask_reviewer(c, contour)
        if review.verdict == "reject":
            return self._close(c)
        if contour == Contour.server:
            return self._owner_gate(c, review)
        return self._merge(c, review)

    def _contour(self, c: Carrier) -> Contour:
        contour = self.workspace.paths.contour(self.workspace.changed_names(c.tree, c.base_sha, c.head_sha))
        current = self._task_contour(c)
        if current != contour:
            with self.store.db.locked_transaction() as conn:
                self._save_in(conn, c, contour=contour if c.release_id else contour.value)
        return contour

    def _task_contour(self, c: Carrier) -> str | None:
        if c.release_id is not None:
            revert = self.store.get_candidate(c.release_id).revert
            return revert.contour.value if revert and revert.contour else None
        contour = self.store.get_task(c.task_id).contour
        return contour.value if contour else None

    def _owner_gate(self, c: Carrier, review: PullRequestReview) -> bool:
        if not c.awaiting_owner and not self._owner_answered(c):
            return self._request_owner(c, review)
        try:
            reviews = self.github.reviews(c.pr_number)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("owner review of %s is not read: %s", c.id, error)
            return False
        login = self.settings.owner_login.lower()
        owner = [
            item for item in reviews
            if str((item.get("user") or {}).get("login", "")).lower() == login
            and item.get("commit_id") == c.head_sha and item.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        ]
        if not owner:
            return False
        last = max(owner, key=lambda item: (str(item.get("submitted_at") or ""), int(item.get("id") or 0)))
        state = "approved" if last["state"] == "APPROVED" else "changes_requested"
        decided = OwnerReview(
            state=state, head_sha=c.head_sha, login=str(last["user"]["login"]), comment=str(last.get("body") or "")[:2000]
        )
        if c.owner_review != decided:
            with self.store.db.locked_transaction() as conn:
                if not self._active_in(conn, c):
                    return False
                self._save_in(conn, c, owner_review=decided)
                self._event(
                    conn, c, TaskEventType.owner_review,
                    {"pr_number": c.pr_number, "head_sha": c.head_sha, "state": state, "login": decided.login},
                )
            c = self._reload(c)
        if state == "changes_requested":
            return self._close(c)
        return self._merge(c, review)

    def _owner_answered(self, c: Carrier) -> bool:
        return (
            not c.merge_sha and c.owner_review is not None and c.owner_review.head_sha == c.head_sha
            and c.owner_review.state != "requested"
        )

    def _request_owner(self, c: Carrier, review: PullRequestReview) -> bool:
        if not self._active(c):
            return False
        key = f"{OWNER_SUMMARY}:{c.id}:{c.head_sha}"
        found = self.ops.find(key)
        if found is not None and found.status == OpStatus.started:
            self.ops.mark(found, OpStatus.failed, detail="restart")
        operation, allowed = self.ops.begin(OWNER_SUMMARY, key, c.task_id, c.release_id)
        if not allowed:
            if operation.status == OpStatus.succeeded:
                return self._owner_requested(None, c)
            return False
        body = f"{self._body(c)}\n## Сводка для владельца\n\n{review.owner_summary or 'нет данных'}\n"
        try:
            self.github.update_pr_body(c.pr_number, body)
        except GitHubUnavailable:
            self.ops.mark(operation, OpStatus.failed, detail="unavailable")
            return False
        except GitHubError as error:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {error.status}")
                if error.status < 500:
                    self._halt_in(
                        conn, c, f"GitHub отклонил запись сводки для владельца в PR #{c.pr_number}: HTTP {error.status}.",
                        step=OWNER_SUMMARY,
                    )
            return False
        return self._owner_requested(operation, c)

    def _owner_requested(self, operation: Operation | None, c: Carrier) -> bool:
        with self.store.db.locked_transaction() as conn:
            if operation is not None:
                self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(c.pr_number))
            if not self._active_in(conn, c):
                return False
            self._save_in(conn, c, owner_review=OwnerReview(state="requested", head_sha=c.head_sha))
            self._event(
                conn, c, TaskEventType.owner_review_requested,
                {"pr_number": c.pr_number, "head_sha": c.head_sha, "pr_url": c.pr_url, "login": self.settings.owner_login},
            )
        return True

    def _owner_facts(self, c: Carrier) -> str:
        changed = self.workspace.changed_names(c.tree, c.base_sha, c.head_sha)
        facts = []
        files = [path for path in DEPENDENCY_FILES if path in changed]
        if files:
            facts.append(f"Изменены манифесты и lock-файлы: {', '.join(files)}.")
        packages = self._changed_packages(c.base_sha, c.head_sha)
        if packages:
            facts.append(
                f"Новые или изменённые зависимости: {', '.join(packages)}. "
                "Их install-скрипты выполнялись в шаге установки зависимостей с сетью."
            )
        if SCHEMA_SQL in changed:
            facts.append(
                f"Изменена схема БД ({SCHEMA_SQL}). Откат Render не возвращает состояние общей БД: "
                "добавленные таблицы и колонки останутся после отката."
            )
        return "\n".join(f"- {fact}" for fact in facts)

    def _changed_packages(self, base: str, head: str) -> list[str]:
        def python_deps(sha: str) -> list[str]:
            data = tomllib.loads((self.workspace.file_at(sha, PYPROJECT) or b"").decode("utf-8"))
            groups = (data.get("dependency-groups") or {}).values()
            items = [*(data.get("project") or {}).get("dependencies", []), *(item for group in groups for item in group)]
            return [item for item in items if isinstance(item, str)]

        found = []
        try:
            before = json.loads(self.workspace.file_at(base, PACKAGE_JSON) or b"{}")
            after = json.loads(self.workspace.file_at(head, PACKAGE_JSON) or b"{}")
            for section in NPM_SECTIONS:
                known = before.get(section) or {}
                found += [f"{name}@{value}" for name, value in (after.get(section) or {}).items() if known.get(name) != value]
            known_python = set(python_deps(base))
            found += [item for item in python_deps(head) if item not in known_python]
        except (ValueError, AttributeError, TypeError):
            return found
        return found

    def _ask_reviewer(self, c: Carrier, contour: Contour = Contour.ui) -> bool:
        if not self._active(c):
            return False
        operation, allowed = self.ops.begin(PR_REVIEW, f"{PR_REVIEW}:{c.id}:{c.head_sha}", c.task_id, c.release_id)
        if not allowed:
            return False
        if c.release_id is None:
            try:
                self.store.reserve_pr_review(c.task_id, self.settings.pr_review_calls)
            except BudgetExceeded:
                return self._review_failed(
                    c, operation, f"Исчерпан предел обращений агента ревью PR: {self.settings.pr_review_calls}."
                )
        reason = None
        try:
            user = self._review_input(c, contour)
            profile = self.store.get_task(c.task_id).profile
            reply = self.gateway.structured(
                self.store, c.task_id, profile, AgentRole.pr_reviewer, PR_REVIEW, PR_REVIEWER, user, PrReview
            )
            if contour == Contour.server and reply.verdict == "approve" and not reply.owner_summary.strip():
                reason = "Агент ревью PR не подготовил сводку для владельца по PR серверного контура; решение не принято, PR открыт."
            facts = self._owner_facts(c) if contour == Contour.server else ""
        except InvalidModelResponse:
            reason = "Ответ агента ревью PR не соответствует схеме или неполон; решение не принято, PR открыт."
        except ProviderError:
            reason = "Агент ревью PR недоступен: основной и резервный провайдеры вернули ошибку; PR открыт."
        except ContextTooLarge as error:
            reason = f"Вход агента ревью PR не помещается в окно модели: {error}"
        except WorkspaceError as error:
            reason = f"Diff для агента ревью PR не получен: {error}"
        if reason is not None:
            return self._review_failed(c, operation, reason)
        summary = reply.owner_summary.strip()
        if facts:
            summary = f"{summary}\n\nФакты офиса:\n{facts}".strip()
        self._apply_review(operation, c, reply, summary or None)
        return True

    def _review_failed(self, c: Carrier, operation: Operation, reason: str) -> bool:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.failed, detail=reason[:300])
            self._halt_in(conn, c, reason, step=PR_REVIEW, role=AgentRole.pr_reviewer)
        return False

    def _apply_review(self, operation: Operation, c: Carrier, reply: PrReview, summary: str | None = None) -> None:
        review_id = new_id("prv")
        with self.store.db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO pr_reviews (id, task_id, head_sha, verdict, reasons, required_changes, checklist, model, "
                "created_at, owner_summary) VALUES (:id, :task_id, :head, :verdict, :reasons, :changes, :checklist, "
                ":model, :now, :summary)",
                {
                    "summary": summary,
                    "id": review_id,
                    "task_id": c.task_id,
                    "head": c.head_sha,
                    "verdict": reply.verdict,
                    "reasons": reply.reasons,
                    "changes": _json(reply.required_changes),
                    "checklist": _json([item.model_dump() for item in reply.checklist]),
                    "model": self._review_model(conn, c.task_id),
                    "now": to_text(utcnow()),
                },
            )
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=review_id)
            self._event(
                conn, c, TaskEventType.pr_review,
                {"review_id": review_id, "head_sha": c.head_sha, "verdict": reply.verdict},
                role=AgentRole.pr_reviewer,
            )

    def _review_model(self, conn: Any, task_id: str) -> str:
        row = conn.fetchone(
            "SELECT payload FROM task_events WHERE task_id = :id AND tool = 'model' AND role = :role "
            "ORDER BY seq DESC LIMIT 1",
            {"id": task_id, "role": AgentRole.pr_reviewer.value},
        )
        arguments = str(json.loads(row["payload"]).get("arguments", "")) if row else ""
        return arguments.split(" ", 1)[-1] or "unknown"

    def _summaries(self, task_id: str) -> dict[AgentRole, str]:
        summaries = {AgentRole.qa: "нет данных", AgentRole.critic: "нет данных"}
        for event in self.store.events_after(task_id, 0, limit=100000):
            if event.type == TaskEventType.role_finished and event.role in summaries:
                summaries[event.role] = str(event.payload.get("summary", ""))
        return summaries

    def _review_input(self, c: Carrier, contour: Contour = Contour.ui) -> str:
        task = self.store.get_task(c.task_id)
        scope = Entry(
            "Контур PR",
            "server — серверный контур: PR сливается только после одобрения владельца, owner_summary обязателен"
            if contour == Contour.server else "ui — контур интерфейса: owner_summary — пустая строка",
            required=True,
        )
        checks = "\n".join(
            f"{check.name}: {check.status} {check.conclusion or ''}".strip()
            for check in (c.checks.checks if c.checks else [])
        )
        diff = self.workspace.diff_range(c.tree, c.base_sha, c.head_sha, REVIEW_DIFF_LIMIT)
        checklist = Entry("Чек-лист", "\n".join(f"- {key}: {label}" for key, label in CHECKLIST.items()), required=True)
        checks_entry = Entry(f"Обязательные проверки PR #{c.pr_number} для head {c.head_sha}", checks, required=True)
        diff_entry = Entry(f"Diff {str(c.base_sha)[:7]}..{str(c.head_sha)[:7]}", diff)
        if c.release_id is not None:
            ranges, reasons = self._revert_scope(self.store.get_candidate(c.release_id))
            fixed = [
                Entry(
                    "Назначение PR",
                    f"Revert выпуска {ranges} задачи «{task.title}» после отката или непрошедших проверок: "
                    "отменяются только слияния PR офиса из этого диапазона, commit владельца остаются.",
                    required=True,
                ),
                Entry("Причина отката", reasons, required=True),
                scope,
                checks_entry,
                checklist,
                Entry(
                    "Изменённые файлы",
                    "\n".join(self.workspace.changed_names(c.tree, c.base_sha, c.head_sha)),
                    required=True,
                ),
                diff_entry,
            ]
        else:
            summaries = self._summaries(task.id)
            spec = task.spec
            criteria = (
                "\n".join(f"{index}. {item}" for index, item in enumerate(spec.acceptance_criteria, 1)) if spec else ""
            )
            fixed = [
                Entry("Текст задачи участника", task.text, required=True),
                Entry(
                    "Требования",
                    f"{spec.summary}\n\nКритерии приёмки:\n{criteria}" if spec else "нет данных",
                    required=True,
                ),
                Entry("Класс задачи", task.change_class.value, required=True),
                scope,
                checks_entry,
                Entry("Итог QA последнего цикла", summaries[AgentRole.qa], required=True),
                Entry("Итог критика последнего цикла", summaries[AgentRole.critic], required=True),
                checklist,
                Entry("Изменённые файлы", "\n".join(task.changed_files), required=True),
                diff_entry,
            ]
        budget = self.gateway.input_budget(task.profile, AgentRole.pr_reviewer)
        return assemble(PR_REVIEWER, fixed, [], budget)

    def _merge(self, c: Carrier, review: PullRequestReview) -> bool:
        number = c.pr_number
        try:
            pr = self.github.get_pr(number)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("PR of %s is not read before merge: %s", c.id, error)
            return False
        head = (pr.get("head") or {}).get("sha")
        if pr.get("state") != "open":
            return self._halt(c, f"PR #{number} закрыт вне офиса до слияния.")
        if head != review.head_sha:
            return self._halt(c, self._stale_review(number, head, review.head_sha))
        with self.store.db.locked_transaction() as conn:
            if not self._active_in(conn, c):
                return False
            operation, allowed = self.ops.begin_in(
                conn, MERGE_PR, f"{MERGE_PR}:{c.id}:{review.head_sha}", c.task_id, c.release_id
            )
            if allowed:
                self._save_in(conn, c)
                self._event(conn, c, TaskEventType.merge_started, {"pr_number": number, "head_sha": review.head_sha})
        if not allowed:
            return False
        result = None
        status = None
        try:
            result = self.github.merge_pr(number, review.head_sha, f"{c.title} (#{number})")
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        sha = result.get("sha") if isinstance(result, dict) and result.get("merged") is True else None
        if isinstance(sha, str) and sha:
            self._apply_merge(operation, c, sha)
            return True
        if status is not None and 400 <= status < 500:
            return self._merge_refused(c, operation, review.head_sha, status)
        return False

    def _stale_review(self, number: int | None, head: Any, reviewed: str) -> str:
        return (
            f"Head PR #{number} изменился после решения агента ревью: {str(head)[:7]} вместо {reviewed[:7]}. "
            "Решение недействительно, слияние не выполнено."
        )

    def _merge_refused(self, c: Carrier, operation: Operation, reviewed: str, status: int) -> bool:
        pr = None
        try:
            pr = self.github.get_pr(c.pr_number)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("PR of %s is not read after refused merge: %s", c.id, error)
        head = (pr.get("head") or {}).get("sha") if pr else None
        if pr is not None and pr.get("merged") and pr.get("merge_commit_sha") and head == reviewed:
            self._apply_merge(operation, c, pr["merge_commit_sha"])
            return True
        if pr is not None and head != reviewed:
            reason = self._stale_review(c.pr_number, head, reviewed)
        else:
            reason = f"GitHub отклонил слияние PR #{c.pr_number}: HTTP {status}."
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {status}")
            self._halt_in(conn, c, reason, step=MERGE_PR)
        return False

    def _apply_merge(self, operation: Operation, c: Carrier, sha: str) -> None:
        with self.store.db.locked_transaction() as conn:
            current = self._carrier_in(conn, c)
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=sha)
            self._save_in(conn, c, merge_sha=sha, checks=None)
            if c.release_id is not None:
                self._fix_targets_in(conn, c.release_id, sha)
            self._event(conn, c, TaskEventType.pr_merged, {"pr_number": current.pr_number, "merge_sha": sha})

    def _fix_targets_in(self, conn: Connection, release_id: str, sha: str) -> None:
        revert = self.store.load_candidate(conn, release_id).revert
        if revert.targets:
            self.store.update_candidate(
                conn, release_id, revert=revert.model_copy(update={"targets": fixed_targets(revert, sha)})
            )

    def _merged_in(self, conn: Connection, c: Carrier, candidate_id: str) -> None:
        self._save_in(conn, c, status="merged", candidate_id=candidate_id)
        self._fix_targets_in(conn, c.release_id, c.merge_sha)

    def _finish(self, c: Carrier) -> bool:
        minutes = f"{self.settings.checks_timeout_minutes:g}"
        sha = c.merge_sha
        required = [(check, check.push_event) for check in self.required if check.push_event]
        if not required:
            return self._halt(c, "Нет обязательных проверок итогового commit: workflow без события push.")
        try:
            summary, run_ids = self._summary(sha, required, lambda run: run.get("head_branch") == "main")
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("checks of merge commit of %s are not read: %s", c.id, error)
            if self._checks_expired(c):
                return self._halt(c, f"Проверки итогового commit не прочитаны за {minutes} мин: {error}.")
            return False
        if c.checks != summary:
            with self.store.db.locked_transaction() as conn:
                self._save_in(conn, c, checks=summary)
                self._event(conn, c, TaskEventType.checks_status, summary.model_dump())
        failed = [
            f"{check.name} ({check.conclusion})"
            for check in summary.checks
            if check.status == "completed" and check.conclusion != "success"
        ]
        if failed:
            broken = any(check.conclusion == "failure" for check in summary.checks if check.status == "completed")
            return self._fail_final(
                c, f"Обязательные проверки итогового commit {sha[:7]} не пройдены: {', '.join(failed)}.",
                revert=broken, run_ids=run_ids,
            )
        waiting = [check.name for check in summary.checks if check.status != "completed"]
        if waiting:
            if self._checks_expired(c):
                return self._halt(
                    c, f"Проверки итогового commit не завершились за {minutes} мин: {', '.join(waiting)}."
                )
            return False
        report = self._final_suite(self._reload(c))
        if report is None:
            return False
        return self._candidate(self._reload(c), report, run_ids)

    def _fail_final(
        self, c: Carrier, reason: str, *, revert: bool = False, run_ids: list[str] | None = None,
        report: CheckReport | None = None,
    ) -> bool:
        if c.release_id is not None or not revert:
            return self._halt(c, reason, step="merge_checks", failure_kind=FailureKind.checks_failed)
        sha = c.merge_sha
        try:
            parent = self.workspace.parent(sha)
        except WorkspaceError as error:
            return self._halt(c, f"{reason} Revert не подготовлен: {error}", step="merge_checks")
        with self.store.db.locked_transaction() as conn:
            if not self._active_in(conn, c) or self._carrier_in(conn, c).merge_sha != sha:
                return False
            task = self.store.load_task(conn, c.task_id)
            created = self.store.create_candidate(
                conn,
                task_id=c.task_id,
                kind=ReleaseKind.release,
                commit_sha=sha,
                expected_previous_sha=parent,
                spec_hash=task.spec.spec_hash if task.spec else "",
                suite_hash=(report.suite_hash if report else None) or "",
                ci_run_ids=run_ids or [],
                pr_url=c.pr_url,
                status=ReleaseStatus.failed,
                e2e=report,
            )
            self.store.update_candidate(conn, created.id, blocked_reason=reason)
            self._halt_in(conn, c, reason, step="merge_checks", failure_kind=FailureKind.checks_failed)
            self.store.set_rework_reason(conn, task, reason)
            self.queue._block_in(conn, created, reason, FailureKind.checks_failed.value)
        return False

    def _final_suite(self, c: Carrier) -> CheckReport | None:
        sha = c.merge_sha
        try:
            with self.workspace.lock:
                self.workspace.git("fetch", "--quiet", "origin", "main", timeout=NETWORK_TIMEOUT)
            playwright = bool(self.workspace.qa_files(sha))
            changed = self.workspace.changed_between(f"{sha}^", sha)
        except WorkspaceError as error:
            self._halt(c, f"Итоговый commit {sha[:7]} не получен: {error}")
            return None
        require = c.release_id is None or playwright
        if c.release_id is None and c.change_class != ChangeClass.backend_api:
            playwright = True
        commands = suite_commands(self.commands, suite_steps(changed, c.change_class, playwright))
        for saved in self.store.check_reports(c.task_id, sha):
            if saved.suite_hash and {check.command for check in saved.checks} >= set(commands):
                return self._judge(c, saved, commands)
        try:
            self.suite.reset_task(c.task_id)
            report = self.suite.run_suite(
                c.task_id, sha, sha, require_e2e=require, diff_base=f"{sha}^", change_class=c.change_class
            )
        except (SandboxError, SandboxCancelled, WorkspaceError, OSError) as error:
            self._halt(c, f"Критический E2E итогового commit {sha[:7]} не выполнен: {error}")
            return None
        return self._judge(c, report, commands)

    def _judge(self, c: Carrier, report: CheckReport, commands: list[str]) -> CheckReport | None:
        sha = c.merge_sha[:7]
        failed = [f"{check.command} (exit {check.exit_code})" for check in report.checks if check.exit_code != 0]
        if any(check.exit_code != 0 and check.command in commands for check in report.checks):
            self._fail_final(
                c, f"Критический E2E итогового commit {sha} не пройден: {', '.join(failed)}.", revert=True, report=report
            )
            return None
        found = {check.command for check in report.checks}
        missing = [f"{command} (не выполнен)" for command in commands if command not in found]
        if failed or missing or not report.suite_hash:
            details = ", ".join(failed + missing) or "нет хеша набора"
            self._halt(c, f"Набор проверок итогового commit {sha} выполнен не полностью: {details}.")
            return None
        return report

    def _candidate(self, c: Carrier, report: CheckReport, run_ids: list[str]) -> bool:
        sha = c.merge_sha
        with self.store.db.connection() as conn:
            existing = self._release_id(conn, c.task_id, sha)
        if existing is not None:
            return c.release_id is not None and self._link(c, existing)
        if self.queue.busy():
            return False
        try:
            previous = self.queue.reconcile_published()
        except QueueBusy:
            return False
        except BaseUnavailable as error:
            return self._halt(c, f"Кандидат выпуска не создан: {error}")
        with self.store.db.locked_transaction() as conn:
            current = self._carrier_in(conn, c)
            if not self._active_in(conn, c) or current.merge_sha != sha:
                return False
            if self._release_id(conn, c.task_id, sha) is not None or self.queue.busy_in(conn):
                return False
            task = self.store.load_task(conn, c.task_id)
            review = self.store.load_review(conn, c.task_id, current.head_sha) if current.head_sha else None
            origin, decision = ReleaseOrigin.task, None
            if c.release_id is not None:
                revert = self.store.load_candidate(conn, c.release_id).revert
                origin = ReleaseOrigin.auto_revert if revert.source == "auto" else ReleaseOrigin.resume
                decision = revert.decision_id
            candidate = self.store.create_candidate(
                conn,
                task_id=c.task_id,
                kind=ReleaseKind.release,
                commit_sha=sha,
                expected_previous_sha=previous,
                spec_hash=task.spec.spec_hash if task.spec else (report.spec_hash or ""),
                suite_hash=report.suite_hash or "",
                ci_run_ids=run_ids,
                pr_url=current.pr_url,
                pr_review_id=review.id if review else None,
                e2e=report,
                origin=origin,
                origin_decision_id=decision,
            )
            if c.release_id is None:
                self.store.transition(conn, c.task_id, S.awaiting_release, expected_from=S.awaiting_integration)
            else:
                self._merged_in(conn, c, candidate.id)
        if c.release_id is not None and c.head_sha:
            with suppress(WorkspaceError):
                self.workspace.remove_worktree(c.tree)
        return True

    def _link(self, c: Carrier, candidate_id: str) -> bool:
        with self.store.db.locked_transaction() as conn:
            current = self._carrier_in(conn, c)
            if not self._active_in(conn, c) or current.merge_sha != c.merge_sha:
                return False
            found = self.store.load_candidate(conn, candidate_id)
            short = c.merge_sha[:7]
            if found.status != ReleaseStatus.pending:
                self._halt_in(
                    conn, c, f"Кандидат выпуска {short} уже создан и находится в статусе {found.status}: укажите другой commit."
                )
                return False
            self._merged_in(conn, c, found.id)
            self._event(
                conn, c, TaskEventType.role_finished,
                {"step": "release", "outcome": "ok", "summary": f"Кандидат выпуска {short} уже создан: выпуск продолжится с ним."},
            )
        if c.head_sha:
            with suppress(WorkspaceError):
                self.workspace.remove_worktree(c.tree)
        return True

    def _release_id(self, conn: Any, task_id: str, sha: str) -> str | None:
        row = conn.fetchone(
            "SELECT id FROM release_candidates WHERE task_id = :id AND commit_sha = :sha AND kind = :kind "
            "ORDER BY CASE status WHEN :pending THEN 0 WHEN :revoked THEN 2 ELSE 1 END, created_at DESC, id DESC "
            "LIMIT 1",
            {
                "id": task_id,
                "sha": sha,
                "kind": ReleaseKind.release.value,
                "pending": ReleaseStatus.pending.value,
                "revoked": ReleaseStatus.revoked.value,
            },
        )
        return row["id"] if row else None

    def _close(self, c: Carrier) -> bool:
        if c.release_id is None:
            if self._status(c.task_id) not in (S.awaiting_integration, S.cancelled):
                return False
        elif not self._active(c):
            return False
        operation, allowed = self.ops.begin(CLOSE_PR, f"{CLOSE_PR}:{c.id}", c.task_id, c.release_id)
        if not allowed:
            return False
        closed = False
        status = None
        try:
            self.github.close_pr(c.pr_number)
            closed = True
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        if closed:
            self._apply_close(operation, c)
            return True
        if status is not None and status < 500:
            self._unknown(c, operation, f"GitHub отклонил закрытие PR #{c.pr_number}: HTTP {status}.")
        elif operation.attempt >= MAX_ATTEMPTS:
            self._unknown(c, operation, f"PR #{c.pr_number} не закрыт за {MAX_ATTEMPTS} попытки.")
        return False

    def _reconcile_review(self, c: Carrier, operation: Operation) -> bool:
        with self.store.db.connection() as conn:
            review = self.store.load_review(conn, c.task_id, operation.key.rsplit(":", 1)[1])
            row = conn.fetchone(
                "SELECT MAX(recorded_at) AS since FROM decisions WHERE operation = 'task_decision' "
                "AND object_id = :id AND decision = 'retry'",
                {"id": c.task_id},
            )
        if review is not None:
            self.ops.mark(operation, OpStatus.succeeded)
            return True
        retried = from_text(row["since"]) if c.release_id is None and row and row["since"] else None
        if retried is not None and retried > operation.started_at:
            self.ops.mark(operation, OpStatus.failed, detail="retry")
            return True
        if operation.status == OpStatus.started:
            self._unknown(
                c, operation,
                "Исход обращения к агенту ревью PR не установлен: ответ мог быть получен, но не сохранён. "
                "Повторное обращение — только после решения «Повторить».",
            )
        return False

    def _reconcile(self, c: Carrier, operation: Operation) -> bool:
        if operation.kind == PR_REVIEW:
            return self._reconcile_review(c, operation)
        outcome: tuple[OpStatus, Any] | None = None
        try:
            outcome = self._verify(c, operation)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("reconcile of %s stopped: %s", operation.key, error)
        if outcome is None:
            limit = timedelta(minutes=self.settings.reconcile_timeout_minutes)
            since = max(operation.started_at, self._last_event(c.task_id, "type = 'status_changed'") or operation.started_at)
            if self.clock() - since > limit:
                self._unknown(c, operation, "Исход операции не установлен: GitHub недоступен.")
            return False
        status, value = outcome
        if status == OpStatus.unknown:
            self._unknown(c, operation, value)
            return False
        if status == OpStatus.failed:
            self.ops.mark(operation, OpStatus.failed, detail=value)
            return True
        if operation.kind == PUSH:
            self._apply_push(operation, c, value)
        elif operation.kind == CREATE_PR:
            self._apply_pr(operation, c, value)
        elif operation.kind == MERGE_PR:
            self._apply_merge(operation, c, value)
        else:
            self._apply_close(operation, c)
        return True

    def _verify(self, c: Carrier, operation: Operation) -> tuple[OpStatus, Any] | None:
        branch = c.branch
        if operation.kind == PUSH:
            sha = operation.key.rsplit(":", 1)[1]
            remote = self.github.branch_sha(branch)
            if remote == sha:
                return OpStatus.succeeded, sha
            if remote is None or self.workspace.is_ancestor(remote, sha):
                return OpStatus.failed, "retry: idempotent"
            return OpStatus.unknown, f"Ветка {branch} на GitHub изменена вне офиса."
        if operation.kind == CREATE_PR:
            found = self.github.pulls(branch, "open")
            if found:
                return OpStatus.succeeded, found[0]
            if self.github.pulls(branch, "closed"):
                return OpStatus.unknown, f"PR ветки {branch} закрыт вне офиса."
            return OpStatus.failed, "not found"
        pr = self.github.get_pr(c.pr_number)
        if operation.kind == MERGE_PR:
            reviewed = operation.key.rsplit(":", 1)[1]
            head = (pr.get("head") or {}).get("sha")
            if pr.get("merged") and pr.get("merge_commit_sha"):
                if head != reviewed:
                    return OpStatus.unknown, self._stale_review(c.pr_number, head, reviewed)
                return OpStatus.succeeded, pr["merge_commit_sha"]
            if pr.get("state") == "closed":
                return OpStatus.unknown, f"PR #{c.pr_number} закрыт без слияния."
            return None
        if pr.get("state") == "closed":
            return OpStatus.succeeded, None
        return OpStatus.failed, "open"

    def _unknown(self, c: Carrier, operation: Operation, reason: str) -> None:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.unknown, detail=reason)
            if c.release_id is not None:
                self._halt_in(conn, c, reason, step=operation.kind)
                return
            current = self.store.load_task(conn, c.task_id)
            if current.status == S.awaiting_integration:
                self.store.halt_in(
                    conn, c.task_id, S.needs_attention, statuses=INTEGRATING, role=None, step=operation.kind,
                    reason=reason,
                )
            elif operation.kind != PUSH:
                self.store.record_event(
                    conn,
                    c.task_id,
                    TaskEventType.operation_unknown,
                    {"kind": operation.kind, "pr_number": current.pr_number, "reason": reason},
                )

    def _apply_push(self, operation: Operation, c: Carrier, sha: str) -> None:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=sha)
            self._save_in(conn, c, head_sha=sha, checks=None)
            self._event(
                conn, c, TaskEventType.tool_called, {"arguments": f"{c.branch} {sha}", "result": "ok"}, tool="git_push"
            )

    def _apply_pr(self, operation: Operation | None, c: Carrier, pr: dict[str, Any]) -> None:
        number = int(pr["number"])
        url = str(pr["html_url"])
        with self.store.db.locked_transaction() as conn:
            if operation is not None:
                self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(number))
            self._save_in(conn, c, pr_number=number, pr_url=url)
            self._event(conn, c, TaskEventType.pr_opened, {"pr_number": number, "pr_url": url})

    def _apply_close(self, operation: Operation, c: Carrier) -> None:
        with self.store.db.locked_transaction() as conn:
            current = self._carrier_in(conn, c)
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(current.pr_number))
            self._event(conn, c, TaskEventType.pr_closed, {"pr_number": current.pr_number})
            if not self._active_in(conn, c):
                return
            owner = current.owner_review
            if owner is not None and owner.state == "changes_requested" and owner.head_sha == current.head_sha:
                self._halt_in(
                    conn, c,
                    f"Владелец {owner.login} отклонил PR #{current.pr_number}: {owner.comment or 'комментарий не указан'}",
                    step="owner_review", failure_kind=FailureKind.owner_rejected,
                )
                return
            review = self.store.load_review(conn, c.task_id, current.head_sha)
            if review is not None and review.verdict == "reject":
                changes = "; ".join(review.required_changes)
                self._halt_in(
                    conn, c, f"Агент ревью отклонил PR #{current.pr_number}: {review.reasons} Что изменить: {changes}",
                    step=PR_REVIEW, role=AgentRole.pr_reviewer, failure_kind=FailureKind.pr_rejected,
                )
                return
            failed = [
                f"{check.name} ({check.conclusion})"
                for check in (current.checks.checks if current.checks else [])
                if check.status == "completed" and check.conclusion != "success"
            ]
            self._halt_in(
                conn, c, f"Обязательные проверки PR не пройдены: {', '.join(failed)}. PR #{current.pr_number} закрыт.",
                step="pr_checks", failure_kind=FailureKind.checks_failed,
            )

    def summary(self, c: Carrier | Task) -> PullRequestChecks:
        required = [(check, check.pr_event) for check in self.required]
        return self._summary(
            c.head_sha,
            required,
            lambda run: any(pr.get("number") == c.pr_number for pr in run.get("pull_requests") or []),
        )[0]

    def _summary(
        self, sha: str, required_checks: list[tuple[RequiredCheck, str]], belongs: Callable[[dict[str, Any]], bool]
    ) -> tuple[PullRequestChecks, list[str]]:
        runs = self.github.runs(sha)
        apps: dict[int, int | None] = {}
        jobs: dict[int, list[dict[str, Any]]] = {}
        checks = []
        counted: list[str] = []
        for required, event in required_checks:
            matching = [
                run for run in runs
                if run.get("path") == required.workflow
                and run.get("event") == event
                and run.get("head_sha") == sha
                and belongs(run)
            ]
            if not matching:
                checks.append(PullRequestCheck(name=required.name, status="missing", conclusion=None, url=None))
                continue
            run = max(matching, key=lambda item: (item["run_number"], item.get("run_attempt", 1)))
            suite = run["check_suite_id"]
            if suite not in apps:
                apps[suite] = self.github.suite_app(suite)
            if apps[suite] != ACTIONS_APP_ID:
                checks.append(PullRequestCheck(name=required.name, status="missing", conclusion=None, url=None))
                continue
            if run["id"] not in jobs:
                jobs[run["id"]] = self.github.jobs(run["id"])
            job = next((item for item in jobs[run["id"]] if item.get("name") == required.name), None)
            if job is None:
                checks.append(
                    PullRequestCheck(name=required.name, status="missing", conclusion=None, url=run.get("html_url"))
                )
                continue
            completed = job.get("status") == "completed"
            checks.append(
                PullRequestCheck(
                    name=required.name,
                    status=str(job.get("status")),
                    conclusion=job.get("conclusion") if completed else None,
                    url=job.get("html_url"),
                )
            )
            if str(run["id"]) not in counted:
                counted.append(str(run["id"]))
        return PullRequestChecks(head_sha=sha, checks=checks), counted

    def _last_event(self, task_id: str, condition: str) -> datetime | None:
        with self.store.db.connection() as conn:
            row = conn.fetchone(
                f"SELECT MAX(occurred_at) AS since FROM task_events WHERE task_id = :id AND ({condition})",
                {"id": task_id},
            )
        return from_text(row["since"]) if row and row["since"] else None

    def _checks_expired(self, c: Carrier) -> bool:
        if c.awaiting_owner or self._owner_answered(c):
            return False
        since = self._last_event(
            c.task_id,
            "type IN ('pr_opened', 'pr_merged', 'decision_recorded') OR (type = 'tool_called' AND tool = 'git_push')",
        )
        return self.clock() - (since or self.clock()) > timedelta(minutes=self.settings.checks_timeout_minutes)

    def _revert_scope(self, release: ReleaseCandidate) -> tuple[str, str]:
        targets = [target for target in targets_of(release) if target.attempt in (None, release.revert.attempt)]
        targets = targets or targets_of(release)
        ranges = ", ".join(f"{target.base_sha[:7]}..{target.head_sha[:7]}" for target in targets)
        reasons = []
        for target in targets:
            found = release if target.release_id == release.id else self.store.get_candidate(target.release_id)
            reasons.append(found.blocked_reason or "нет данных")
        return ranges, "\n\n".join(dict.fromkeys(reasons))

    def _body(self, c: Carrier) -> str:
        task = self.store.get_task(c.task_id)
        if c.release_id is not None:
            release = self.store.get_candidate(c.release_id)
            ranges, reasons = self._revert_scope(release)
            return (
                f"## Исправление после отката\n\nRevert выпуска `{release.id}` ({ranges}) задачи «{task.title}»: "
                "отменяются только слияния PR офиса, commit владельца остаются.\n\n"
                f"## Причина отката\n\n{reasons}\n\n"
                f"Задача офиса: `{task.id}`.\n"
            )
        summaries = self._summaries(task.id)
        goal = task.spec.summary if task.spec else task.text
        criteria = "\n".join(f"- {item}" for item in task.spec.acceptance_criteria) if task.spec else "- нет данных"
        return (
            f"## Цель\n\n{goal}\n\n## Критерии приёмки\n\n{criteria}\n\n"
            f"## Итог QA\n\n{summaries[AgentRole.qa]}\n\n## Итог критика\n\n{summaries[AgentRole.critic]}\n\n"
            f"Задача офиса: `{task.id}`.\n"
        )
