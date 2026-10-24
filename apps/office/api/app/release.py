import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.branch_check import WorkspaceError
from app.config import Settings
from app.db import from_text, to_text, utcnow
from app.github import ACTIONS_APP_ID, GitHubClient, GitHubError, GitHubUnavailable
from app.operations import Operation, OperationLog, OpStatus
from app.required_checks import RequiredCheck
from app.schemas import AgentRole, FailureKind, PullRequestCheck, PullRequestChecks, Task, TaskEventType, TaskStatus
from app.tasks import SIGNAL_DECISION_RECORDED, SIGNAL_SLOT_RELEASED, SIGNAL_TASK_CLOSED, TaskStore, _json
from app.workspace import PushResult, Workspace, branch_name

logger = logging.getLogger("office.release")

S = TaskStatus
PUSH = "push"
CREATE_PR = "create_pr"
CLOSE_PR = "close_pr"
MAX_ATTEMPTS = 3
STEPS_PER_PASS = 8
SHUTDOWN_TIMEOUT = 30.0
INTEGRATING = frozenset({S.awaiting_integration})
CANDIDATES = (
    "SELECT id, created_at FROM tasks WHERE status = 'awaiting_integration' "
    "UNION SELECT t.id, t.created_at FROM tasks t WHERE t.status = 'cancelled' "
    "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.type = 'operation_unknown') "
    "AND ((t.pr_number IS NOT NULL AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id "
    "AND o.kind = 'close_pr' AND o.status = 'succeeded')) "
    "OR EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id AND o.kind IN ('create_pr', 'close_pr') "
    "AND o.status IN ('started', 'unknown'))) "
    "ORDER BY created_at, id"
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
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace = workspace
        self.github = github
        self.required = required
        self.clock = clock
        self.ops = OperationLog(store.db)
        self._wake = threading.Condition()
        self._pending = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        store.signal_handlers.append(self.on_signal)

    def on_signal(self, name: str, task_id: str) -> None:
        if name in (SIGNAL_SLOT_RELEASED, SIGNAL_DECISION_RECORDED, SIGNAL_TASK_CLOSED):
            self.wake()

    def wake(self) -> None:
        with self._wake:
            self._pending = True
            self._wake.notify_all()

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
            for _ in range(STEPS_PER_PASS):
                if self._stopping:
                    return
                try:
                    if not self.step(task_id):
                        break
                except (GitHubError, GitHubUnavailable) as error:
                    logger.warning("release step of %s stopped: %s", task_id, error)
                    break
                except Exception:
                    logger.exception("release step failed: %s", task_id)
                    break

    def step(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        if task.status not in (S.awaiting_integration, S.cancelled):
            return False
        pending = self.ops.open_for(task_id)
        if task.status == S.cancelled:
            pending = [operation for operation in pending if operation.kind != PUSH]
        if pending:
            return self._reconcile(task, pending[0])
        if task.status == S.cancelled:
            closed = self.ops.find(f"{CLOSE_PR}:{task_id}")
            if task.pr_number is None or (closed is not None and closed.status == OpStatus.succeeded):
                return False
            return self._close(task)
        return self._integrate(task)

    def _status(self, task_id: str) -> TaskStatus:
        return self.store.get_task(task_id).status

    def _halt(self, task: Task, reason: str) -> bool:
        self.store.halt(task.id, S.needs_attention, statuses=INTEGRATING, role=None, step="release", reason=reason)
        return False

    def _integrate(self, task: Task) -> bool:
        try:
            head = self.workspace.head(task.id)
        except WorkspaceError as error:
            return self._halt(task, f"Рабочая копия задачи недоступна: {error}")
        if task.head_sha != head:
            return self._push(task, head)
        if task.pr_number is None:
            return self._open_pr(task)
        return self._check(task)

    def _push(self, task: Task, head: str) -> bool:
        try:
            found = self.workspace.verify_branch(task.id)
        except WorkspaceError as error:
            found = [str(error)]
        if found:
            return self._halt(task, f"Повторная проверка ветки перед push не пройдена: {'; '.join(found)}")
        token = self.github.auth.token()
        if self._status(task.id) != S.awaiting_integration:
            return False
        operation, allowed = self.ops.begin(PUSH, f"{PUSH}:{task.id}:{head}", task.id)
        if not allowed:
            if operation.status == OpStatus.succeeded:
                self._apply_push(operation, task.id, head)
                return True
            return False
        result = self.workspace.push(task.id, self.settings.push_url, token, head)
        if result == PushResult.ok:
            self._apply_push(operation, task.id, head)
            return True
        if result == PushResult.rejected:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail="rejected")
                self.store.halt_in(
                    conn, task.id, S.needs_attention, statuses=INTEGRATING, role=None, step="git_push",
                    reason=f"GitHub отклонил push ветки {branch_name(task.id)}: ветка изменена вне офиса или push запрещён.",
                )
            return False
        if operation.attempt >= MAX_ATTEMPTS:
            self._unknown(task, operation, f"Push ветки {branch_name(task.id)} не выполнен за {MAX_ATTEMPTS} попытки.")
        return False

    def _open_pr(self, task: Task) -> bool:
        branch = branch_name(task.id)
        found = self.github.pulls(branch, "open")
        if found:
            self._apply_pr(None, task.id, found[0])
            return True
        if self._status(task.id) != S.awaiting_integration:
            return False
        body = self._body(task)
        operation, allowed = self.ops.begin(CREATE_PR, f"{CREATE_PR}:{task.id}", task.id)
        if not allowed:
            return False
        pr = None
        status = None
        try:
            pr = self.github.create_pr(branch, task.title, body)
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        if pr is not None:
            self._apply_pr(operation, task.id, pr)
            return True
        if status is not None and status < 500:
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {status}")
                self.store.halt_in(
                    conn, task.id, S.needs_attention, statuses=INTEGRATING, role=None, step="create_pr",
                    reason=f"GitHub отклонил создание PR: HTTP {status}.",
                )
            return False
        if operation.attempt >= MAX_ATTEMPTS:
            self._unknown(task, operation, f"PR не создан за {MAX_ATTEMPTS} попытки.")
        return False

    def _check(self, task: Task) -> bool:
        minutes = f"{self.settings.checks_timeout_minutes:g}"
        try:
            pr = self.github.get_pr(task.pr_number)
            head = (pr.get("head") or {}).get("sha")
            if pr.get("state") != "open":
                return self._halt(task, f"PR #{task.pr_number} закрыт вне офиса.")
            if head != task.head_sha:
                if self.github.branch_sha(branch_name(task.id)) != task.head_sha:
                    return self._halt(
                        task, f"Ветка PR #{task.pr_number} изменена вне офиса: head {str(head)[:7]} вместо "
                        f"{task.head_sha[:7]}."
                    )
                if self._checks_expired(task):
                    return self._halt(task, f"Head PR #{task.pr_number} не обновился за {minutes} мин.")
                return False
            summary = self.summary(task)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("checks of %s are not read: %s", task.id, error)
            if self._checks_expired(task):
                return self._halt(task, f"Обязательные проверки PR не прочитаны за {minutes} мин: {error}.")
            return False
        if task.checks != summary:
            with self.store.db.locked_transaction() as conn:
                conn.execute(
                    "UPDATE tasks SET checks = :checks, version = version + 1, updated_at = :now WHERE id = :id",
                    {"checks": _json(summary.model_dump()), "now": to_text(utcnow()), "id": task.id},
                )
                self.store.record_event(conn, task.id, TaskEventType.checks_status, summary.model_dump())
        failed = [check for check in summary.checks if check.status == "completed" and check.conclusion != "success"]
        if failed:
            return self._close(self.store.get_task(task.id))
        waiting = [check.name for check in summary.checks if check.status != "completed"]
        if waiting and self._checks_expired(task):
            return self._halt(task, f"Обязательные проверки PR не завершились за {minutes} мин: {', '.join(waiting)}.")
        return False

    def _close(self, task: Task) -> bool:
        if self._status(task.id) not in (S.awaiting_integration, S.cancelled):
            return False
        operation, allowed = self.ops.begin(CLOSE_PR, f"{CLOSE_PR}:{task.id}", task.id)
        if not allowed:
            return False
        closed = False
        status = None
        try:
            self.github.close_pr(task.pr_number)
            closed = True
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        if closed:
            self._apply_close(operation, task.id)
            return True
        if status is not None and status < 500:
            self._unknown(task, operation, f"GitHub отклонил закрытие PR #{task.pr_number}: HTTP {status}.")
        elif operation.attempt >= MAX_ATTEMPTS:
            self._unknown(task, operation, f"PR #{task.pr_number} не закрыт за {MAX_ATTEMPTS} попытки.")
        return False

    def _reconcile(self, task: Task, operation: Operation) -> bool:
        outcome: tuple[OpStatus, Any] | None = None
        try:
            outcome = self._verify(task, operation)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("reconcile of %s stopped: %s", operation.key, error)
        if outcome is None:
            limit = timedelta(minutes=self.settings.reconcile_timeout_minutes)
            since = max(operation.started_at, self._last_event(task.id, "type = 'status_changed'") or operation.started_at)
            if self.clock() - since > limit:
                self._unknown(task, operation, "Исход операции не установлен: GitHub недоступен.")
            return False
        status, value = outcome
        if status == OpStatus.unknown:
            self._unknown(task, operation, value)
            return False
        if status == OpStatus.failed:
            self.ops.mark(operation, OpStatus.failed, detail=value)
            return True
        if operation.kind == PUSH:
            self._apply_push(operation, task.id, value)
        elif operation.kind == CREATE_PR:
            self._apply_pr(operation, task.id, value)
        else:
            self._apply_close(operation, task.id)
        return True

    def _verify(self, task: Task, operation: Operation) -> tuple[OpStatus, Any]:
        branch = branch_name(task.id)
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
        pr = self.github.get_pr(task.pr_number)
        if pr.get("state") == "closed":
            return OpStatus.succeeded, None
        return OpStatus.failed, "open"

    def _unknown(self, task: Task, operation: Operation, reason: str) -> None:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.unknown, detail=reason)
            current = self.store.load_task(conn, task.id)
            if current.status == S.awaiting_integration:
                self.store.halt_in(
                    conn, task.id, S.needs_attention, statuses=INTEGRATING, role=None, step=operation.kind,
                    reason=reason,
                )
            elif operation.kind != PUSH:
                self.store.record_event(
                    conn,
                    task.id,
                    TaskEventType.operation_unknown,
                    {"kind": operation.kind, "pr_number": current.pr_number, "reason": reason},
                )

    def _apply_push(self, operation: Operation, task_id: str, sha: str) -> None:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=sha)
            conn.execute(
                "UPDATE tasks SET head_sha = :sha, checks = NULL, version = version + 1, updated_at = :now "
                "WHERE id = :id",
                {"sha": sha, "now": to_text(utcnow()), "id": task_id},
            )
            self.store.record_event(
                conn,
                task_id,
                TaskEventType.tool_called,
                {"arguments": f"{branch_name(task_id)} {sha}", "result": "ok"},
                tool="git_push",
            )

    def _apply_pr(self, operation: Operation | None, task_id: str, pr: dict[str, Any]) -> None:
        number = int(pr["number"])
        url = str(pr["html_url"])
        with self.store.db.locked_transaction() as conn:
            if operation is not None:
                self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(number))
            conn.execute(
                "UPDATE tasks SET pr_number = :number, pr_url = :url, version = version + 1, updated_at = :now "
                "WHERE id = :id",
                {"number": number, "url": url, "now": to_text(utcnow()), "id": task_id},
            )
            self.store.record_event(conn, task_id, TaskEventType.pr_opened, {"pr_number": number, "pr_url": url})

    def _apply_close(self, operation: Operation, task_id: str) -> None:
        with self.store.db.locked_transaction() as conn:
            task = self.store.load_task(conn, task_id)
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=str(task.pr_number))
            self.store.record_event(conn, task_id, TaskEventType.pr_closed, {"pr_number": task.pr_number})
            if task.status != S.awaiting_integration:
                return
            failed = [
                f"{check.name} ({check.conclusion})"
                for check in (task.checks.checks if task.checks else [])
                if check.status == "completed" and check.conclusion != "success"
            ]
            self.store.halt_in(
                conn, task_id, S.failed, statuses=INTEGRATING, role=None, step="pr_checks",
                reason=f"Обязательные проверки PR не пройдены: {', '.join(failed)}. PR #{task.pr_number} закрыт.",
                failure_kind=FailureKind.checks_failed,
            )

    def summary(self, task: Task) -> PullRequestChecks:
        runs = self.github.runs(task.head_sha)
        apps: dict[int, int | None] = {}
        jobs: dict[int, list[dict[str, Any]]] = {}
        checks = []
        for required in self.required:
            matching = [
                run for run in runs
                if run.get("path") == required.workflow
                and run.get("event") == required.pr_event
                and run.get("head_sha") == task.head_sha
                and any(pr.get("number") == task.pr_number for pr in run.get("pull_requests") or [])
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
        return PullRequestChecks(head_sha=task.head_sha, checks=checks)

    def _last_event(self, task_id: str, condition: str) -> datetime | None:
        with self.store.db.connection() as conn:
            row = conn.fetchone(
                f"SELECT MAX(occurred_at) AS since FROM task_events WHERE task_id = :id AND ({condition})",
                {"id": task_id},
            )
        return from_text(row["since"]) if row and row["since"] else None

    def _checks_expired(self, task: Task) -> bool:
        since = self._last_event(task.id, "type = 'pr_opened' OR (type = 'tool_called' AND tool = 'git_push')")
        return self.clock() - (since or task.updated_at) > timedelta(minutes=self.settings.checks_timeout_minutes)

    def _body(self, task: Task) -> str:
        summaries = {AgentRole.qa: "нет данных", AgentRole.critic: "нет данных"}
        for event in self.store.events_after(task.id, 0, limit=100000):
            if event.type == TaskEventType.role_finished and event.role in summaries:
                summaries[event.role] = str(event.payload.get("summary", ""))
        goal = task.spec.summary if task.spec else task.text
        criteria = "\n".join(f"- {item}" for item in task.spec.acceptance_criteria) if task.spec else "- нет данных"
        return (
            f"## Цель\n\n{goal}\n\n## Критерии приёмки\n\n{criteria}\n\n"
            f"## Итог QA\n\n{summaries[AgentRole.qa]}\n\n## Итог критика\n\n{summaries[AgentRole.critic]}\n\n"
            f"Задача офиса: `{task.id}`.\n"
        )
