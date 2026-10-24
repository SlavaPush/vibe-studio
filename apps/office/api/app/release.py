import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.branch_check import WorkspaceError
from app.config import Settings
from app.db import from_text, to_text, utcnow
from app.github import ACTIONS_APP_ID, GitHubClient, GitHubError, GitHubUnavailable
from app.models_gateway import InvalidModelResponse, ModelsGateway, ProviderError
from app.operations import Operation, OperationLog, OpStatus
from app.required_checks import RequiredCheck
from app.roles import CHECKLIST, PR_REVIEWER, ContextTooLarge, Entry, PrReview, assemble
from app.sandbox import SUITE, CommandsPolicy, SandboxCancelled, SandboxError
from app.schemas import (
    AgentRole,
    CheckReport,
    FailureKind,
    PullRequestCheck,
    PullRequestChecks,
    PullRequestReview,
    ReleaseKind,
    Task,
    TaskEventType,
    TaskStatus,
)
from app.tasks import (
    SIGNAL_DECISION_RECORDED,
    SIGNAL_SLOT_RELEASED,
    SIGNAL_TASK_CLOSED,
    BudgetExceeded,
    TaskStore,
    _json,
    new_id,
)
from app.workspace import NETWORK_TIMEOUT, PushResult, Workspace, branch_name

logger = logging.getLogger("office.release")

S = TaskStatus
PUSH = "push"
CREATE_PR = "create_pr"
CLOSE_PR = "close_pr"
PR_REVIEW = "pr_review"
MERGE_PR = "merge_pr"
MAX_ATTEMPTS = 3
STEPS_PER_PASS = 8
SHUTDOWN_TIMEOUT = 30.0
REVIEW_DIFF_LIMIT = 200 * 1024
SERVICE_TIMEOUT = 20.0
SHORT_SHA = re.compile(r"[0-9a-f]{7,40}")
INTEGRATING = frozenset({S.awaiting_integration})
CANDIDATES = (
    "SELECT id, created_at FROM tasks WHERE status = 'awaiting_integration' "
    "UNION SELECT t.id, t.created_at FROM tasks t WHERE t.status = 'cancelled' "
    "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.type = 'operation_unknown') "
    "AND ((t.pr_number IS NOT NULL AND t.merge_sha IS NULL AND NOT EXISTS (SELECT 1 FROM operations o "
    "WHERE o.task_id = t.id AND o.kind = 'close_pr' AND o.status = 'succeeded')) "
    "OR EXISTS (SELECT 1 FROM operations o WHERE o.task_id = t.id "
    "AND o.kind IN ('create_pr', 'close_pr', 'merge_pr') AND o.status IN ('started', 'unknown'))) "
    "ORDER BY created_at, id"
)


class BaseUnavailable(Exception):
    pass


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
            pending = [operation for operation in pending if operation.kind not in (PUSH, PR_REVIEW)]
        if pending:
            return self._reconcile(task, pending[0])
        if task.status == S.cancelled:
            closed = self.ops.find(f"{CLOSE_PR}:{task_id}")
            if task.merge_sha or task.pr_number is None or (closed is not None and closed.status == OpStatus.succeeded):
                return False
            return self._close(task)
        return self._integrate(task)

    def _status(self, task_id: str) -> TaskStatus:
        return self.store.get_task(task_id).status

    def _halt(self, task: Task, reason: str) -> bool:
        self.store.halt(task.id, S.needs_attention, statuses=INTEGRATING, role=None, step="release", reason=reason)
        return False

    def _integrate(self, task: Task) -> bool:
        if task.merge_sha:
            return self._finish(task)
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
        if waiting:
            if self._checks_expired(task):
                return self._halt(
                    task, f"Обязательные проверки PR не завершились за {minutes} мин: {', '.join(waiting)}."
                )
            return False
        return self._review(self.store.get_task(task.id))

    def _review(self, task: Task) -> bool:
        try:
            found = self.workspace.verify_branch(task.id)
        except WorkspaceError as error:
            found = [str(error)]
        if found:
            return self._halt(task, f"Повторная проверка ветки перед ревью PR не пройдена: {'; '.join(found)}")
        with self.store.db.connection() as conn:
            review = self.store.load_review(conn, task.id, task.head_sha)
        if review is None:
            return self._ask_reviewer(task)
        if review.verdict == "reject":
            return self._close(task)
        return self._merge(task, review)

    def _ask_reviewer(self, task: Task) -> bool:
        if self._status(task.id) != S.awaiting_integration:
            return False
        operation, allowed = self.ops.begin(PR_REVIEW, f"{PR_REVIEW}:{task.id}:{task.head_sha}", task.id)
        if not allowed:
            return False
        try:
            self.store.reserve_pr_review(task.id, self.settings.pr_review_calls)
        except BudgetExceeded:
            return self._review_failed(
                task, operation, f"Исчерпан предел обращений агента ревью PR: {self.settings.pr_review_calls}."
            )
        reason = None
        try:
            user = self._review_input(task)
            reply = self.gateway.structured(
                self.store, task.id, task.profile, AgentRole.pr_reviewer, PR_REVIEW, PR_REVIEWER, user, PrReview
            )
        except InvalidModelResponse:
            reason = "Ответ агента ревью PR не соответствует схеме или неполон; решение не принято, PR открыт."
        except ProviderError:
            reason = "Агент ревью PR недоступен: основной и резервный провайдеры вернули ошибку; PR открыт."
        except ContextTooLarge as error:
            reason = f"Вход агента ревью PR не помещается в окно модели: {error}"
        except WorkspaceError as error:
            reason = f"Diff для агента ревью PR не получен: {error}"
        if reason is not None:
            return self._review_failed(task, operation, reason)
        self._apply_review(operation, task, reply)
        return True

    def _review_failed(self, task: Task, operation: Operation, reason: str) -> bool:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.failed, detail=reason[:300])
            self.store.halt_in(
                conn, task.id, S.needs_attention, statuses=INTEGRATING, role=AgentRole.pr_reviewer, step=PR_REVIEW,
                reason=reason,
            )
        return False

    def _apply_review(self, operation: Operation, task: Task, reply: PrReview) -> None:
        review_id = new_id("prv")
        with self.store.db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO pr_reviews (id, task_id, head_sha, verdict, reasons, required_changes, checklist, model, "
                "created_at) VALUES (:id, :task_id, :head, :verdict, :reasons, :changes, :checklist, :model, :now)",
                {
                    "id": review_id,
                    "task_id": task.id,
                    "head": task.head_sha,
                    "verdict": reply.verdict,
                    "reasons": reply.reasons,
                    "changes": _json(reply.required_changes),
                    "checklist": _json([item.model_dump() for item in reply.checklist]),
                    "model": self._review_model(conn, task.id),
                    "now": to_text(utcnow()),
                },
            )
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=review_id)
            self.store.record_event(
                conn,
                task.id,
                TaskEventType.pr_review,
                {"review_id": review_id, "head_sha": task.head_sha, "verdict": reply.verdict},
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

    def _review_input(self, task: Task) -> str:
        summaries = {AgentRole.qa: "нет данных", AgentRole.critic: "нет данных"}
        for event in self.store.events_after(task.id, 0, limit=100000):
            if event.type == TaskEventType.role_finished and event.role in summaries:
                summaries[event.role] = str(event.payload.get("summary", ""))
        spec = task.spec
        criteria = "\n".join(f"{index}. {item}" for index, item in enumerate(spec.acceptance_criteria, 1)) if spec else ""
        checks = "\n".join(
            f"{check.name}: {check.status} {check.conclusion or ''}".strip()
            for check in (task.checks.checks if task.checks else [])
        )
        diff = self.workspace.diff_range(task.id, task.base_sha, task.head_sha, REVIEW_DIFF_LIMIT)
        fixed = [
            Entry("Текст задачи участника", task.text, required=True),
            Entry(
                "Требования",
                f"{spec.summary}\n\nКритерии приёмки:\n{criteria}" if spec else "нет данных",
                required=True,
            ),
            Entry(f"Обязательные проверки PR #{task.pr_number} для head {task.head_sha}", checks, required=True),
            Entry("Итог QA последнего цикла", summaries[AgentRole.qa], required=True),
            Entry("Итог критика последнего цикла", summaries[AgentRole.critic], required=True),
            Entry("Чек-лист", "\n".join(f"- {key}: {label}" for key, label in CHECKLIST.items()), required=True),
            Entry("Изменённые файлы", "\n".join(task.changed_files), required=True),
            Entry(f"Diff {str(task.base_sha)[:7]}..{str(task.head_sha)[:7]}", diff),
        ]
        budget = self.gateway.input_budget(task.profile, AgentRole.pr_reviewer)
        return assemble(PR_REVIEWER, fixed, [], budget)

    def _merge(self, task: Task, review: PullRequestReview) -> bool:
        number = task.pr_number
        try:
            pr = self.github.get_pr(number)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("PR of %s is not read before merge: %s", task.id, error)
            return False
        head = (pr.get("head") or {}).get("sha")
        if pr.get("state") != "open":
            return self._halt(task, f"PR #{number} закрыт вне офиса до слияния.")
        if head != review.head_sha:
            return self._halt(task, self._stale_review(number, head, review.head_sha))
        with self.store.db.locked_transaction() as conn:
            if self.store.load_task(conn, task.id).status != S.awaiting_integration:
                return False
            operation, allowed = self.ops.begin_in(conn, MERGE_PR, f"{MERGE_PR}:{task.id}:{review.head_sha}", task.id)
            if allowed:
                conn.execute(
                    "UPDATE tasks SET version = version + 1, updated_at = :now WHERE id = :id",
                    {"now": to_text(utcnow()), "id": task.id},
                )
                self.store.record_event(
                    conn, task.id, TaskEventType.merge_started, {"pr_number": number, "head_sha": review.head_sha}
                )
        if not allowed:
            return False
        result = None
        status = None
        try:
            result = self.github.merge_pr(number, review.head_sha, f"{task.title} (#{number})")
        except GitHubUnavailable:
            pass
        except GitHubError as error:
            status = error.status
        sha = result.get("sha") if isinstance(result, dict) and result.get("merged") is True else None
        if isinstance(sha, str) and sha:
            self._apply_merge(operation, task.id, sha)
            return True
        if status is not None and 400 <= status < 500:
            return self._merge_refused(task, operation, review.head_sha, status)
        return False

    def _stale_review(self, number: int | None, head: Any, reviewed: str) -> str:
        return (
            f"Head PR #{number} изменился после решения агента ревью: {str(head)[:7]} вместо {reviewed[:7]}. "
            "Решение недействительно, слияние не выполнено."
        )

    def _merge_refused(self, task: Task, operation: Operation, reviewed: str, status: int) -> bool:
        pr = None
        try:
            pr = self.github.get_pr(task.pr_number)
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("PR of %s is not read after refused merge: %s", task.id, error)
        head = (pr.get("head") or {}).get("sha") if pr else None
        if pr is not None and pr.get("merged") and pr.get("merge_commit_sha") and head == reviewed:
            self._apply_merge(operation, task.id, pr["merge_commit_sha"])
            return True
        if pr is not None and head != reviewed:
            reason = self._stale_review(task.pr_number, head, reviewed)
        else:
            reason = f"GitHub отклонил слияние PR #{task.pr_number}: HTTP {status}."
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {status}")
            self.store.halt_in(
                conn, task.id, S.needs_attention, statuses=INTEGRATING, role=None, step=MERGE_PR, reason=reason,
            )
        return False

    def _apply_merge(self, operation: Operation, task_id: str, sha: str) -> None:
        with self.store.db.locked_transaction() as conn:
            task = self.store.load_task(conn, task_id)
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=sha)
            conn.execute(
                "UPDATE tasks SET merge_sha = :sha, checks = NULL, version = version + 1, updated_at = :now "
                "WHERE id = :id",
                {"sha": sha, "now": to_text(utcnow()), "id": task_id},
            )
            self.store.record_event(
                conn, task_id, TaskEventType.pr_merged, {"pr_number": task.pr_number, "merge_sha": sha}
            )

    def _finish(self, task: Task) -> bool:
        minutes = f"{self.settings.checks_timeout_minutes:g}"
        sha = task.merge_sha
        required = [(check, check.push_event) for check in self.required if check.push_event]
        if not required:
            return self._halt(task, "Нет обязательных проверок итогового commit: workflow без события push.")
        try:
            summary, run_ids = self._summary(sha, required, lambda run: run.get("head_branch") == "main")
        except (GitHubError, GitHubUnavailable) as error:
            logger.warning("checks of merge commit of %s are not read: %s", task.id, error)
            if self._checks_expired(task):
                return self._halt(task, f"Проверки итогового commit не прочитаны за {minutes} мин: {error}.")
            return False
        if task.checks != summary:
            with self.store.db.locked_transaction() as conn:
                conn.execute(
                    "UPDATE tasks SET checks = :checks, version = version + 1, updated_at = :now WHERE id = :id",
                    {"checks": _json(summary.model_dump()), "now": to_text(utcnow()), "id": task.id},
                )
                self.store.record_event(conn, task.id, TaskEventType.checks_status, summary.model_dump())
        failed = [
            f"{check.name} ({check.conclusion})"
            for check in summary.checks
            if check.status == "completed" and check.conclusion != "success"
        ]
        if failed:
            return self._fail_final(task, f"Обязательные проверки итогового commit {sha[:7]} не пройдены: {', '.join(failed)}.")
        waiting = [check.name for check in summary.checks if check.status != "completed"]
        if waiting:
            if self._checks_expired(task):
                return self._halt(
                    task, f"Проверки итогового commit не завершились за {minutes} мин: {', '.join(waiting)}."
                )
            return False
        report = self._final_suite(self.store.get_task(task.id))
        if report is None:
            return False
        return self._candidate(self.store.get_task(task.id), report, run_ids)

    def _fail_final(self, task: Task, reason: str) -> bool:
        self.store.halt(
            task.id, S.failed, statuses=INTEGRATING, role=None, step="merge_checks", reason=reason,
            failure_kind=FailureKind.checks_failed,
        )
        return False

    def _final_suite(self, task: Task) -> CheckReport | None:
        sha = task.merge_sha
        commands = [self.commands.steps[name].command for name in SUITE]
        for saved in self.store.check_reports(task.id, sha):
            if saved.suite_hash and {check.command for check in saved.checks} >= set(commands):
                return self._judge(task, saved, commands)
        try:
            with self.workspace.lock:
                self.workspace.git("fetch", "--quiet", "origin", "main", timeout=NETWORK_TIMEOUT)
            self.suite.reset_task(task.id)
            report = self.suite.run_suite(task.id, sha, sha, require_e2e=True)
        except (SandboxError, SandboxCancelled, WorkspaceError, OSError) as error:
            self._halt(task, f"Критический E2E итогового commit {sha[:7]} не выполнен: {error}")
            return None
        return self._judge(task, report, commands)

    def _judge(self, task: Task, report: CheckReport, commands: list[str]) -> CheckReport | None:
        sha = task.merge_sha[:7]
        failed = [f"{check.command} (exit {check.exit_code})" for check in report.checks if check.exit_code != 0]
        if any(check.exit_code != 0 and check.command in commands for check in report.checks):
            self._fail_final(task, f"Критический E2E итогового commit {sha} не пройден: {', '.join(failed)}.")
            return None
        found = {check.command for check in report.checks}
        missing = [f"{command} (не выполнен)" for command in commands if command not in found]
        if failed or missing or not report.suite_hash:
            details = ", ".join(failed + missing) or "нет хеша набора"
            self._halt(task, f"Набор проверок итогового commit {sha} выполнен не полностью: {details}.")
            return None
        return report

    def _candidate(self, task: Task, report: CheckReport, run_ids: list[str]) -> bool:
        sha = task.merge_sha
        with self.store.db.connection() as conn:
            if self._release_exists(conn, task.id, sha):
                return False
        try:
            previous = self._published_base()
        except BaseUnavailable as error:
            return self._halt(task, f"Кандидат выпуска не создан: {error}")
        with self.store.db.locked_transaction() as conn:
            current = self.store.load_task(conn, task.id)
            if current.status != S.awaiting_integration or current.merge_sha != sha:
                return False
            if self._release_exists(conn, task.id, sha):
                return False
            review = self.store.load_review(conn, task.id, current.head_sha)
            self.store.create_candidate(
                conn,
                task_id=task.id,
                kind=ReleaseKind.release,
                commit_sha=sha,
                expected_previous_sha=previous,
                spec_hash=current.spec.spec_hash if current.spec else (report.spec_hash or ""),
                suite_hash=report.suite_hash or "",
                ci_run_ids=run_ids,
                pr_url=current.pr_url,
                pr_review_id=review.id if review else None,
                e2e=report,
            )
            self.store.transition(conn, task.id, S.awaiting_release, expected_from=S.awaiting_integration)
        return True

    def _release_exists(self, conn: Any, task_id: str, sha: str) -> bool:
        row = conn.fetchone(
            "SELECT id FROM release_candidates WHERE task_id = :id AND commit_sha = :sha AND kind = :kind",
            {"id": task_id, "sha": sha, "kind": ReleaseKind.release.value},
        )
        return row is not None

    def _published_base(self) -> str:
        version = self._service_version()
        with self.store.db.connection() as conn:
            row = conn.fetchone(
                "SELECT commit_sha FROM release_candidates WHERE is_published = :yes ORDER BY created_at DESC, id DESC "
                "LIMIT 1",
                {"yes": True},
            )
        if row is not None:
            published = row["commit_sha"]
            if not published.startswith(version):
                raise BaseUnavailable(
                    f"версия сервиса {version} не совпадает с последним опубликованным выпуском {published[:7]}."
                )
            return published
        try:
            full = self.github.commit(version).get("sha")
            compared = self.github.compare(str(full), "main") if isinstance(full, str) else {}
        except (GitHubError, GitHubUnavailable) as error:
            raise BaseUnavailable(f"commit опубликованной версии {version} не найден: {error}.") from None
        if not isinstance(full, str) or not full.startswith(version):
            raise BaseUnavailable(f"commit опубликованной версии {version} не определён однозначно.")
        if compared.get("status") not in ("ahead", "identical") or compared.get("behind_by") != 0:
            raise BaseUnavailable(f"опубликованная версия {version} не входит в историю main.")
        return full

    def _service_version(self) -> str:
        url = f"{self.settings.service_url.rstrip('/')}/api/version"
        try:
            response = self.service.get(url, headers={"Cache-Control": "no-cache"})
        except httpx.HTTPError:
            raise BaseUnavailable("версия сервиса недоступна: ответ не получен.") from None
        if response.status_code != 200:
            raise BaseUnavailable(f"версия сервиса недоступна: HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            data = None
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not SHORT_SHA.fullmatch(version):
            raise BaseUnavailable("ответ версии сервиса не содержит SHA commit.")
        return version

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

    def _reconcile_review(self, task: Task, operation: Operation) -> bool:
        with self.store.db.connection() as conn:
            review = self.store.load_review(conn, task.id, operation.key.rsplit(":", 1)[1])
            row = conn.fetchone(
                "SELECT MAX(recorded_at) AS since FROM decisions WHERE operation = 'task_decision' "
                "AND object_id = :id AND decision = 'retry'",
                {"id": task.id},
            )
        if review is not None:
            self.ops.mark(operation, OpStatus.succeeded)
            return True
        retried = from_text(row["since"]) if row and row["since"] else None
        if retried is not None and retried > operation.started_at:
            self.ops.mark(operation, OpStatus.failed, detail="retry")
            return True
        if operation.status == OpStatus.started:
            self._unknown(
                task, operation,
                "Исход обращения к агенту ревью PR не установлен: ответ мог быть получен, но не сохранён. "
                "Повторное обращение — только после решения «Повторить».",
            )
        return False

    def _reconcile(self, task: Task, operation: Operation) -> bool:
        if operation.kind == PR_REVIEW:
            return self._reconcile_review(task, operation)
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
        elif operation.kind == MERGE_PR:
            self._apply_merge(operation, task.id, value)
        else:
            self._apply_close(operation, task.id)
        return True

    def _verify(self, task: Task, operation: Operation) -> tuple[OpStatus, Any] | None:
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
        if operation.kind == MERGE_PR:
            reviewed = operation.key.rsplit(":", 1)[1]
            head = (pr.get("head") or {}).get("sha")
            if pr.get("merged") and pr.get("merge_commit_sha"):
                if head != reviewed:
                    return OpStatus.unknown, self._stale_review(task.pr_number, head, reviewed)
                return OpStatus.succeeded, pr["merge_commit_sha"]
            if pr.get("state") == "closed":
                return OpStatus.unknown, f"PR #{task.pr_number} закрыт без слияния."
            return None
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
            review = self.store.load_review(conn, task_id, task.head_sha)
            if review is not None and review.verdict == "reject":
                changes = "; ".join(review.required_changes)
                self.store.halt_in(
                    conn, task_id, S.failed, statuses=INTEGRATING, role=AgentRole.pr_reviewer, step=PR_REVIEW,
                    reason=f"Агент ревью отклонил PR #{task.pr_number}: {review.reasons} Что изменить: {changes}",
                    failure_kind=FailureKind.pr_rejected,
                )
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
        required = [(check, check.pr_event) for check in self.required]
        return self._summary(
            task.head_sha,
            required,
            lambda run: any(pr.get("number") == task.pr_number for pr in run.get("pull_requests") or []),
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

    def _checks_expired(self, task: Task) -> bool:
        since = self._last_event(
            task.id, "type IN ('pr_opened', 'pr_merged') OR (type = 'tool_called' AND tool = 'git_push')"
        )
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
