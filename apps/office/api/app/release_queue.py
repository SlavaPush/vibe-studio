import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from app.config import Settings
from app.db import Connection, from_text, to_text
from app.decisions import decide_release
from app.github import GitHubClient, GitHubError, GitHubUnavailable
from app.operations import Operation, OperationLog, OpStatus
from app.permissions import not_allowed, require_admin
from app.render import FAILED, LIVE, RenderClient, RenderError, RenderUnavailable, is_terminal
from app.schemas import (
    ACTIVE_RELEASE_STATUSES,
    REWORK_SOURCES,
    Decision,
    DeployResult,
    FailureKind,
    Participant,
    ReleaseCandidate,
    ReleaseDecision,
    ReleaseDecisionRequest,
    ReleaseKind,
    ReleaseOrigin,
    ReleaseStatus,
    RevertResult,
    RevertTarget,
    RollbackResult,
    TaskEventType,
    TaskStatus,
    VerifyResult,
)
from app.service_check import SERVICE_TIMEOUT, BaseUnavailable, ServiceCheck
from app.tasks import TRANSITIONS, TaskStore, candidate_from_row

logger = logging.getLogger("office.release")

R = ReleaseStatus
S = TaskStatus
DEPLOY = "deploy"
ROLLBACK = "rollback"
QUEUE_OPS = (DEPLOY, ROLLBACK)
VERIFY_INTERVAL = 5.0
CLOCK_SKEW = timedelta(minutes=1)
ACTIVITY_PAGES = 10
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
RELEASABLE = frozenset({S.awaiting_release, S.deploying, S.needs_attention})


class QueueBusy(Exception):
    pass


def commit_of(deploy: dict[str, Any] | None) -> str | None:
    return ((deploy or {}).get("commit") or {}).get("id")


def created_of(deploy: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(deploy.get("createdAt")))
    except ValueError:
        return EPOCH


def new_revert(
    source: str = "auto", fix_sha: str | None = None, decision_id: str | None = None,
    targets: list[RevertTarget] | None = None,
) -> RevertResult:
    return RevertResult(
        status="pending", source=source, base_sha=None, head_sha=None, pr_number=None, pr_url=None, checks=None,
        fix_sha=fix_sha, decision_id=decision_id, candidate_id=None, reason=None, targets=targets or [],
    )


def targets_of(release: ReleaseCandidate) -> list[RevertTarget]:
    if release.revert is None:
        return []
    if release.revert.targets:
        return list(release.revert.targets)
    source = "rolled_back" if release.status == ReleaseStatus.rolled_back else "verify_failed"
    return [
        RevertTarget(
            release_id=release.id, source=source, base_sha=release.expected_previous_sha, head_sha=release.commit_sha
        )
    ]


def fixes_of(release: ReleaseCandidate) -> list[str] | None:
    revert = release.revert
    if revert is None or revert.status != "merged" or not revert.fix_sha:
        return None
    if not revert.targets:
        return [revert.fix_sha]
    if any(target.fixed_by is None for target in revert.targets):
        return None
    return list(dict.fromkeys([target.fixed_by for target in revert.targets] + [revert.fix_sha]))


def fixed_targets(revert: RevertResult, sha: str) -> list[RevertTarget]:
    return [
        target.model_copy(update={"fixed_by": sha, "resumed": revert.source == "resume"})
        if target.fixed_by is None and target.attempt == revert.attempt else target
        for target in revert.targets
    ]


class ReleaseQueue:
    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        github: GitHubClient,
        render: RenderClient | None,
        service: ServiceCheck,
        clock: Callable[[], datetime],
        ops: OperationLog,
        pause: Callable[[float], bool],
    ) -> None:
        self.settings = settings
        self.store = store
        self.github = github
        self.render = render
        self.service = service
        self.clock = clock
        self.ops = ops
        self.pause = pause
        self._contains: dict[tuple[str, str], bool] = {}

    def busy_in(self, conn: Connection) -> bool:
        if self.store.queue_state(conn)["active_release_id"]:
            return True
        row = conn.fetchone(
            "SELECT id FROM operations WHERE kind IN ('deploy', 'rollback') AND status IN ('started', 'unknown') "
            "LIMIT 1"
        )
        return row is not None

    def busy(self) -> bool:
        with self.store.db.connection() as conn:
            return self.busy_in(conn)

    def contains(self, head: str, sha: str) -> bool:
        if head == sha:
            return True
        if (head, sha) not in self._contains:
            data = self.github.compare(sha, head)
            self._contains[(head, sha)] = data.get("status") == "ahead" and data.get("behind_by") == 0
        return self._contains[(head, sha)]

    def _candidates(self, conn: Connection, condition: str, params: dict[str, Any]) -> list[ReleaseCandidate]:
        rows = conn.fetchall(f"SELECT * FROM release_candidates WHERE {condition} ORDER BY created_at, id", params)
        return [candidate_from_row(row) for row in rows]

    def _pending(self) -> list[ReleaseCandidate]:
        with self.store.db.connection() as conn:
            return self._candidates(conn, "status = :status", {"status": R.pending.value})

    def _main_commit(self, version: str) -> str:
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

    def _left(self, deadline: datetime) -> float:
        return max(min(SERVICE_TIMEOUT, (deadline - self.clock()).total_seconds()), 0.0)

    def published_version(self) -> str:
        deadline = self.clock() + timedelta(seconds=self.settings.verify_seconds)
        while True:
            try:
                return self.service.version(self._left(deadline))
            except BaseUnavailable:
                if self.clock() >= deadline:
                    raise
            if not self.pause(min(VERIFY_INTERVAL, self._left(deadline))):
                raise QueueBusy()

    def reconcile_published(self) -> str:
        version = self.published_version()
        with self.store.db.connection() as conn:
            released = conn.fetchall(
                "SELECT * FROM release_candidates WHERE status = :status ORDER BY is_published DESC, "
                "created_at DESC, id DESC",
                {"status": R.released.value},
            )
            published_sha = self.store.queue_state(conn)["published_sha"]
        match = next((item for item in map(candidate_from_row, released) if item.commit_sha.startswith(version)), None)
        if match is not None:
            full = match.commit_sha
        elif published_sha and published_sha.startswith(version):
            full = published_sha
        else:
            full = self._main_commit(version)
        stale = [
            item for item in self._pending()
            if item.kind == ReleaseKind.release and self.contains(full, item.commit_sha)
        ]
        with self.store.db.locked_transaction() as conn:
            if self.busy_in(conn):
                raise QueueBusy()
            self._set_published(conn, match, full)
            for item in stale:
                current = self.store.load_candidate(conn, item.id)
                if current.status != R.pending:
                    continue
                self.store.update_candidate(
                    conn, item.id, status=R.superseded.value, superseded_by=match.id if match else None
                )
                note = "" if match else " (внешний деплой)"
                self._settle(conn, current, S.released, f"Опубликовано в составе {full[:7]}{note}.")
        return full

    def _set_published(self, conn: Connection, match: ReleaseCandidate | None, full: str) -> None:
        state = self.store.queue_state(conn)
        keep = match.id if match else ""
        conn.execute(
            "UPDATE release_candidates SET is_published = :no, version = version + 1 "
            "WHERE is_published = :yes AND id <> :id",
            {"no": False, "yes": True, "id": keep},
        )
        if match is not None:
            conn.execute(
                "UPDATE release_candidates SET is_published = :yes, version = version + 1 "
                "WHERE id = :id AND is_published = :no",
                {"no": False, "yes": True, "id": match.id},
            )
            if state["published_sha"]:
                self.store.set_queue(conn, published_sha=None)
            return
        if state["published_sha"] == full:
            return
        self.store.set_queue(conn, published_sha=full)
        waiting = self._candidates(conn, "status = :status", {"status": R.pending.value})
        for task_id in dict.fromkeys(item.task_id for item in waiting):
            self.store.record_event(conn, task_id, TaskEventType.external_publish, {"published_sha": full})

    def _settle(
        self, conn: Connection, candidate: ReleaseCandidate, target: TaskStatus, reason: str,
        failure_kind: FailureKind | None = None,
    ) -> None:
        task = self.store.load_task(conn, candidate.task_id)
        outcome = {S.released: "ok", S.needs_attention: "blocked"}.get(target, "error")
        self.store.record_event(
            conn, task.id, TaskEventType.role_finished,
            {"step": "release", "outcome": outcome, "summary": reason[:300], "release_id": candidate.id},
        )
        movable = (
            candidate.kind == ReleaseKind.release
            and candidate.origin == ReleaseOrigin.task
            and task.status in RELEASABLE
            and target != task.status
            and target in TRANSITIONS[task.status]
        )
        if not movable:
            return
        if failure_kind is not None:
            conn.execute(
                "UPDATE tasks SET failure_kind = :kind WHERE id = :id", {"kind": failure_kind.value, "id": task.id}
            )
        if failure_kind in REWORK_SOURCES:
            self.store.set_rework_reason(conn, task, reason)
        self.store.transition(conn, task.id, target, expected_from=task.status, reason=reason)

    def contained(self, candidate: ReleaseCandidate) -> frozenset[str]:
        with self.store.db.connection() as conn:
            blocked = self.store.queue_state(conn)["release_blocked_by"]
            owner = self.store.load_candidate(conn, blocked) if blocked else None
        fixes = fixes_of(owner) if owner is not None else None
        found = frozenset(sha for sha in fixes or [] if self.contains(candidate.commit_sha, sha))
        if fixes and set(fixes) <= found:
            self._backfill(owner)
        return found

    def _backfill(self, owner: ReleaseCandidate) -> None:
        revert = owner.revert
        legacy = not revert.targets
        if legacy and revert.source == "resume":
            return
        targets = [
            target.model_copy(update={"attempt": revert.attempt, "fixed_by": revert.fix_sha}) if legacy else target
            for target in targets_of(owner)
        ]
        missing = [target for target in targets if target.merges is None and not target.resumed]
        if not missing and not legacy:
            return
        merges = {target.release_id: self.office_merges(target.base_sha, target.head_sha) for target in missing}
        filled = [
            target.model_copy(update={"merges": merges[target.release_id]}) if target.release_id in merges else target
            for target in targets
        ]
        with self.store.db.locked_transaction() as conn:
            current = self.store.load_candidate(conn, owner.id)
            if current.version != owner.version:
                return
            self.store.update_candidate(conn, owner.id, revert=revert.model_copy(update={"targets": filled}))
            for target in missing:
                release = self.store.load_candidate(conn, target.release_id)
                reason = (release.blocked_reason if release else None) or "Изменение отменено revert."
                for sha in merges[target.release_id]:
                    task_id = self.store.merge_task(conn, sha)
                    if task_id is not None:
                        self.store.fail_merged_in(conn, task_id, FailureKind(target.source), reason)

    def office_merges(self, base: str, head: str) -> list[str]:
        data = self.github.compare(base, head)
        parents = {
            item["sha"]: [parent["sha"] for parent in item.get("parents") or []] for item in data.get("commits") or []
        }
        line = []
        sha = head
        while sha != base and parents.get(sha):
            line.append(sha)
            sha = parents[sha][0]
        return [sha for sha in line if self.office_merge(sha)]

    def _halt_in(self, conn: Connection, candidate: ReleaseCandidate, reason: str) -> None:
        self.store.update_candidate(conn, candidate.id, blocked_reason=reason)
        self._settle(conn, candidate, S.needs_attention, reason)

    def _halt(self, candidate: ReleaseCandidate, reason: str) -> bool:
        with self.store.db.locked_transaction() as conn:
            self._halt_in(conn, candidate, reason)
        return False

    def _is_active(self, conn: Connection, candidate: ReleaseCandidate, status: ReleaseStatus) -> bool:
        current = self.store.load_candidate(conn, candidate.id)
        return (
            self.store.queue_state(conn)["active_release_id"] == candidate.id
            and current.status == status
            and not current.blocked_reason
        )

    def step(self) -> bool:
        if self.render is None:
            return False
        for operation in self.ops.open_kinds(QUEUE_OPS):
            if operation.status == OpStatus.unknown:
                return False
            return self._reconcile(operation)
        with self.store.db.connection() as conn:
            active_id = self.store.queue_state(conn)["active_release_id"]
            active = self.store.load_candidate(conn, active_id) if active_id else None
        if active is None:
            return self._next()
        if active.blocked_reason or active.status not in ACTIVE_RELEASE_STATUSES:
            return False
        if active.status == R.deploying:
            return self._poll(active)
        if active.status == R.verifying:
            return self._verify(active)
        return self._poll_rollback(active)

    def _field(self, operation: Operation, candidate: ReleaseCandidate) -> str:
        return "rollback" if operation.kind == ROLLBACK and candidate.kind == ReleaseKind.release else "deploy"

    def _reconcile(self, operation: Operation) -> bool:
        candidate = self.store.get_candidate(operation.release_id)
        field = self._field(operation, candidate)
        target = candidate.rollback.target_sha if field == "rollback" else candidate.commit_sha
        action = "отката" if operation.kind == ROLLBACK else "деплоя"
        try:
            deploys = self.render.list_deploys()
        except (RenderError, RenderUnavailable) as error:
            logger.warning("reconcile of %s stopped: %s", operation.key, error)
            if self.clock() - operation.started_at > timedelta(minutes=self.settings.reconcile_timeout_minutes):
                self._unknown(candidate, operation, f"Исход запроса {action} не установлен: Render недоступен.")
            return False
        since = operation.started_at - CLOCK_SKEW
        found = [
            deploy for deploy in deploys
            if commit_of(deploy) == target
            and created_of(deploy) >= since
            and (deploy.get("trigger") == "rollback") == (operation.kind == ROLLBACK)
        ]
        if len(found) != 1:
            detail = "не найден" if not found else f"найден {len(found)} раза"
            self._unknown(
                candidate, operation,
                f"Исход запроса {action} не установлен: деплой {target[:7]} после начала операции {detail} в "
                "списке Render. Повторного запроса нет; продолжение — решение «Возобновить выпуск» после сверки.",
            )
            return False
        self._started(candidate, operation, field, found[0])
        return True

    def _unknown(self, candidate: ReleaseCandidate, operation: Operation, reason: str) -> None:
        with self.store.db.locked_transaction() as conn:
            self.ops.finish(conn, operation, OpStatus.unknown, detail=reason[:300])
            self._halt_in(conn, candidate, reason)

    def _started(self, candidate: ReleaseCandidate, operation: Operation, field: str, deploy: dict[str, Any]) -> None:
        deploy_id = str(deploy["id"])
        status = str(deploy.get("status"))
        now = self.clock()
        with self.store.db.locked_transaction() as conn:
            current = self.store.load_candidate(conn, candidate.id)
            self.ops.finish(conn, operation, OpStatus.succeeded, external_ref=deploy_id)
            if field == "deploy":
                result = DeployResult(
                    render_deploy_id=deploy_id, render_status=status, started_at=now, finished_at=None,
                    published_sha=None, error=None,
                )
                self.store.update_candidate(conn, candidate.id, deploy=result)
                event = TaskEventType.deploy_status
            else:
                rollback = current.rollback.model_copy(update={"render_deploy_id": deploy_id, "render_status": status})
                self.store.update_candidate(conn, candidate.id, rollback=rollback)
                event = TaskEventType.rollback_status
            self.store.record_event(
                conn, candidate.task_id, event,
                {"release_id": candidate.id, "render_deploy_id": deploy_id, "render_status": status},
            )

    def _call(
        self, candidate: ReleaseCandidate, operation: Operation, field: str, request: Callable[[], dict[str, Any]]
    ) -> bool:
        try:
            deploy = request()
        except RenderUnavailable as error:
            logger.warning("render request of %s is not answered: %s", candidate.id, error)
            return False
        except RenderError as error:
            if error.status >= 500:
                logger.warning("render request of %s failed: %s", candidate.id, error)
                return False
            with self.store.db.locked_transaction() as conn:
                self.ops.finish(conn, operation, OpStatus.failed, detail=f"HTTP {error.status}")
                if field == "rollback":
                    self._halt_in(conn, candidate, f"Render отклонил откат: HTTP {error.status}.")
                    return False
                current = self.store.load_candidate(conn, candidate.id)
                self.store.update_candidate(conn, candidate.id, status=R.failed.value)
                self.store.set_queue(conn, active_release_id=None)
                self._restore_superseded(conn, current)
                self._settle(
                    conn, current, S.failed, f"Render отклонил запрос деплоя: HTTP {error.status}.",
                    FailureKind.deploy_failed,
                )
            return False
        if not isinstance(deploy.get("id"), str):
            return False
        self._started(candidate, operation, field, deploy)
        return True

    def _track(self, candidate: ReleaseCandidate, field: str, deploy: dict[str, Any]) -> bool:
        status = str(deploy.get("status"))
        saved = candidate.deploy if field == "deploy" else candidate.rollback
        if saved.render_status == status:
            return False
        finished = self.clock() if is_terminal(status) else None
        with self.store.db.locked_transaction() as conn:
            self.store.update_candidate(
                conn, candidate.id, **{field: saved.model_copy(update={"render_status": status, "finished_at": finished})}
            )
            self.store.record_event(
                conn, candidate.task_id,
                TaskEventType.deploy_status if field == "deploy" else TaskEventType.rollback_status,
                {"release_id": candidate.id, "render_deploy_id": saved.render_deploy_id, "render_status": status},
            )
        return True

    def _overtaken(self, own: dict[str, Any], deploys: list[dict[str, Any]]) -> bool:
        since = created_of(own)
        return any(deploy.get("id") != own.get("id") and created_of(deploy) > since for deploy in deploys)

    def _poll(self, candidate: ReleaseCandidate) -> bool:
        if candidate.deploy is None:
            return False
        deploy = self.render.get_deploy(candidate.deploy.render_deploy_id)
        deploys = self.render.list_deploys()
        changed = self._track(candidate, "deploy", deploy)
        status = str(deploy.get("status"))
        if self._overtaken(deploy, deploys):
            return self._halt(
                candidate, "Во время деплоя в Render появился более новый деплой вне офиса: проверка и откат не "
                "выполняются.",
            )
        if status == LIVE:
            with self.store.db.locked_transaction() as conn:
                if not self._is_active(conn, candidate, R.deploying):
                    return False
                self.store.update_candidate(conn, candidate.id, status=R.verifying.value)
            return True
        if status == "deactivated":
            return self._halt(candidate, "Деплой офиса деактивирован вне офиса: проверка и откат не выполняются.")
        if status in FAILED:
            return self._deploy_failed(candidate, status)
        return changed

    def _deploy_failed(self, candidate: ReleaseCandidate, status: str) -> bool:
        try:
            version = self.service.version()
        except BaseUnavailable:
            version = None
        if version is None or not candidate.expected_previous_sha.startswith(version):
            return self._halt(
                candidate,
                f"Деплой Render завершился статусом {status}, а опубликованная версия {version or 'не определена'} "
                f"не совпадает с прежней {candidate.expected_previous_sha[:7]}.",
            )
        with self.store.db.locked_transaction() as conn:
            if not self._is_active(conn, candidate, R.deploying):
                return False
            current = self.store.load_candidate(conn, candidate.id)
            deploy = current.deploy.model_copy(update={"error": f"Render: {status}"})
            self.store.update_candidate(conn, candidate.id, status=R.failed.value, deploy=deploy)
            self.store.set_queue(conn, active_release_id=None)
            self._restore_superseded(conn, current)
            self._settle(
                conn, current, S.failed,
                f"Деплой Render завершился статусом {status}; отката не было, опубликована прежняя версия.",
                FailureKind.deploy_failed,
            )
        return True

    def _check(self, expected: str) -> VerifyResult | None:
        deadline = self.clock() + timedelta(seconds=self.settings.verify_seconds)
        attempts = 0
        while True:
            attempts += 1
            health = self.service.health(self._left(deadline))
            try:
                version = self.service.version(self._left(deadline))
            except BaseUnavailable:
                version = None
            page = self.service.page_version(self._left(deadline))
            problems = []
            if not health:
                problems.append("/healthz не ответил status ok")
            if version is None or not expected.startswith(version):
                problems.append(f"/api/version — {version or 'нет ответа'} вместо {expected[:7]}")
            if page is None or not expected.startswith(page):
                problems.append(f"meta app-version — {page or 'нет'} вместо {expected[:7]}")
            if not problems or self.clock() >= deadline:
                return VerifyResult(
                    expected_sha=expected, ok=not problems, fault=False, attempts=attempts, health=health,
                    version=version, page_version=page, error="; ".join(problems) or None, checked_at=self.clock(),
                )
            if not self.pause(min(VERIFY_INTERVAL, self._left(deadline))):
                return None

    def _verify(self, candidate: ReleaseCandidate) -> bool:
        if self.settings.verify_fault and candidate.kind == ReleaseKind.release and candidate.origin == ReleaseOrigin.task:
            result = VerifyResult(
                expected_sha=candidate.commit_sha, ok=False, fault=True, attempts=0, health=False, version=None,
                page_version=None, error="Управляемый сбой проверки (OFFICE_VERIFY_FAULT=1): сервис не проверялся.",
                checked_at=self.clock(),
            )
        else:
            result = self._check(candidate.commit_sha)
        if result is None:
            return False
        own = self.render.get_deploy(candidate.deploy.render_deploy_id)
        deploys = self.render.list_deploys()
        with self.store.db.locked_transaction() as conn:
            if not self._is_active(conn, candidate, R.verifying):
                return False
            deploy = candidate.deploy.model_copy(update={"published_sha": result.version if result.ok else None})
            self.store.update_candidate(conn, candidate.id, verify=result, deploy=deploy)
            self.store.record_event(
                conn, candidate.task_id, TaskEventType.deploy_status,
                {
                    "release_id": candidate.id, "render_deploy_id": candidate.deploy.render_deploy_id,
                    "render_status": str(own.get("status")), "verified": result.ok, "fault": result.fault,
                },
            )
        if own.get("status") != LIVE or self._overtaken(own, deploys):
            return self._halt(
                candidate, "Во время проверки после деплоя в Render появился деплой вне офиса: отката нет."
            )
        if result.ok:
            contained = self.contained(candidate)
            with self.store.db.locked_transaction() as conn:
                if not self._is_active(conn, candidate, R.verifying):
                    return False
                self._publish_in(conn, self.store.load_candidate(conn, candidate.id), contained)
            return True
        if candidate.kind == ReleaseKind.rollback:
            return self._halt(candidate, f"Проверка после ручного отката не пройдена: {result.error}")
        return self._start_rollback(self.store.get_candidate(candidate.id))

    def _publish_in(self, conn: Connection, candidate: ReleaseCandidate, contained: frozenset[str]) -> None:
        state = self.store.queue_state(conn)
        short = candidate.commit_sha[:7]
        self.store.update_candidate(conn, candidate.id, status=R.released.value, blocked_reason=None)
        self._set_published(conn, candidate, candidate.commit_sha)
        self.store.set_queue(conn, active_release_id=None)
        if candidate.kind == ReleaseKind.rollback:
            self._rolled_back_manually(conn, candidate)
            return
        self._settle(conn, candidate, S.released, f"Опубликовано: {short}, проверка после деплоя пройдена.")
        for item in self._candidates(conn, "superseded_by = :id", {"id": candidate.id}):
            self._settle(conn, item, S.released, f"Опубликовано в составе {short}.")
        blocked = state["release_blocked_by"]
        source = self.store.load_candidate(conn, blocked) if blocked else None
        fixes = fixes_of(source) if source is not None else None
        if source is None or not fixes or not set(fixes) <= contained:
            return
        self.store.set_queue(conn, release_blocked_by=None)
        for task_id in dict.fromkeys((source.task_id, candidate.task_id)):
            self.store.record_event(
                conn, task_id, TaskEventType.release_unblocked, {"release_id": blocked, "fixed_by": candidate.id}
            )
        self._rework_in(conn, source)

    def _rework_in(self, conn: Connection, owner: ReleaseCandidate) -> None:
        targets = owner.revert.targets if owner.revert.targets or owner.revert.source == "resume" else targets_of(owner)
        for target in targets:
            if target.resumed:
                continue
            release = self.store.load_candidate(conn, target.release_id)
            for sha in target.merges or []:
                task_id = self.store.merge_task(conn, sha)
                if task_id is None:
                    continue
                context = {
                    "source": target.source, "release_id": target.release_id, "owner_id": owner.id, "merge_sha": sha,
                    "comment": release.reason if release else None, "materials": None,
                }
                self.store.start_rework_in(conn, task_id, owner.id, context)
        self.store._promote(conn)

    def _rolled_back_manually(self, conn: Connection, candidate: ReleaseCandidate) -> None:
        row = conn.fetchone(
            "SELECT * FROM release_candidates WHERE kind = :kind AND status = :status AND commit_sha = :sha "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            {"kind": ReleaseKind.release.value, "status": R.released.value, "sha": candidate.expected_previous_sha},
        )
        reason = (
            f"Ручной откат администратора: {candidate.reason or 'причина не указана'}. Опубликована прежняя версия "
            f"{candidate.commit_sha[:7]} вместо {candidate.expected_previous_sha[:7]}."
        )
        self.store.record_event(
            conn, candidate.task_id, TaskEventType.rollback_status,
            {
                "release_id": candidate.id, "render_deploy_id": candidate.deploy.render_deploy_id if candidate.deploy
                else None, "render_status": LIVE, "verified": True,
            },
        )
        if row is None:
            return
        selected = candidate_from_row(row)
        self.store.update_candidate(
            conn, selected.id, status=R.rolled_back.value, blocked_reason=reason, reason=candidate.reason
        )
        self._block_in(conn, selected, reason, "rolled_back")
        for item in [selected] + self._candidates(conn, "superseded_by = :id", {"id": selected.id}):
            if item.kind == ReleaseKind.release and item.origin == ReleaseOrigin.task:
                self.store.fail_merged_in(conn, item.task_id, FailureKind.rolled_back, reason)

    def _block_in(self, conn: Connection, candidate: ReleaseCandidate, reason: str, source: str) -> None:
        target = RevertTarget(
            release_id=candidate.id, source=source, base_sha=candidate.expected_previous_sha,
            head_sha=candidate.commit_sha,
        )
        blocked = self.store.queue_state(conn)["release_blocked_by"]
        owner = self.store.load_candidate(conn, blocked) if blocked else None
        if owner is None or owner.revert is None:
            self.store.update_candidate(conn, candidate.id, revert=new_revert(targets=[target]))
            self.store.set_queue(conn, release_blocked_by=candidate.id)
            owner = candidate
        else:
            targets = targets_of(owner)
            if candidate.id not in {item.release_id for item in targets}:
                revert = owner.revert.model_copy(update={"targets": targets + [target]})
                self.store.update_candidate(conn, owner.id, revert=revert)
        self.store.record_event(
            conn, candidate.task_id, TaskEventType.release_blocked,
            {"release_id": candidate.id, "blocked_by": owner.id, "reason": reason[:300]},
        )

    def _start_rollback(self, candidate: ReleaseCandidate) -> bool:
        try:
            version = self.service.version()
        except BaseUnavailable:
            version = None
        deploys = self.render.list_deploys()
        latest = max(deploys, key=created_of, default=None)
        own = latest is not None and latest.get("id") == candidate.deploy.render_deploy_id
        if not own or (version is not None and not candidate.commit_sha.startswith(version)):
            return self._halt(
                candidate,
                f"Перед откатом обнаружен деплой вне офиса или версия сервиса {version} вместо "
                f"{candidate.commit_sha[:7]}: откат не выполняется.",
            )
        target = self._target_deploy(candidate.expected_previous_sha, deploys)
        if target is None:
            return self._halt(
                candidate, f"Деплой прежней версии {candidate.expected_previous_sha[:7]} для отката не найден в Render."
            )
        with self.store.db.locked_transaction() as conn:
            if not self._is_active(conn, candidate, R.verifying):
                return False
            rollback = RollbackResult(
                target_sha=candidate.expected_previous_sha, target_deploy_id=target, render_deploy_id=None,
                render_status=None, started_at=self.clock(), finished_at=None, verify=None, error=None,
            )
            self.store.update_candidate(conn, candidate.id, status=R.rolling_back.value, rollback=rollback)
            operation, allowed = self.ops.begin_in(conn, ROLLBACK, f"{ROLLBACK}:{candidate.id}", candidate.task_id, candidate.id)
            self.store.record_event(
                conn, candidate.task_id, TaskEventType.rollback_status,
                {"release_id": candidate.id, "render_deploy_id": None, "render_status": "requested", "target_sha": rollback.target_sha},
            )
        if not allowed:
            return False
        return self._call(candidate, operation, "rollback", lambda: self.render.rollback(target))

    def _target_deploy(self, sha: str, deploys: list[dict[str, Any]]) -> str | None:
        with self.store.db.connection() as conn:
            known = self._candidates(conn, "commit_sha = :sha AND deploy IS NOT NULL", {"sha": sha})
        if known:
            return known[-1].deploy.render_deploy_id
        found = [deploy for deploy in deploys if commit_of(deploy) == sha and deploy.get("status") in (LIVE, "deactivated")]
        return str(max(found, key=created_of)["id"]) if found else None

    def _poll_rollback(self, candidate: ReleaseCandidate) -> bool:
        rollback = candidate.rollback
        if rollback is None or rollback.render_deploy_id is None:
            return False
        deploy = self.render.get_deploy(rollback.render_deploy_id)
        deploys = self.render.list_deploys()
        changed = self._track(candidate, "rollback", deploy)
        status = str(deploy.get("status"))
        if self._overtaken(deploy, deploys):
            return self._halt(candidate, "Во время отката в Render появился деплой вне офиса.")
        if status in FAILED:
            return self._halt(candidate, f"Откат Render завершился статусом {status}.")
        if status != LIVE:
            return changed
        result = self._check(rollback.target_sha)
        if result is None:
            return False
        deploy = self.render.get_deploy(rollback.render_deploy_id)
        deploys = self.render.list_deploys()
        with self.store.db.locked_transaction() as conn:
            if not self._is_active(conn, candidate, R.rolling_back):
                return False
            current = self.store.load_candidate(conn, candidate.id)
            done = current.rollback.model_copy(update={"verify": result, "error": result.error})
            self.store.update_candidate(conn, candidate.id, rollback=done)
            if deploy.get("status") != LIVE or self._overtaken(deploy, deploys):
                self._halt_in(conn, current, "Во время проверки отката в Render появился деплой вне офиса.")
                return False
            if not result.ok:
                self._halt_in(conn, current, f"Проверка после отката не пройдена: {result.error}")
                return False
            reason = (
                f"Проверка после деплоя {current.commit_sha[:7]} не пройдена: "
                f"{current.verify.error if current.verify else 'нет данных'}. "
                f"Выполнен откат к {rollback.target_sha[:7]}."
            )
            self.store.update_candidate(conn, candidate.id, status=R.failed.value, blocked_reason=reason)
            self.store.set_queue(conn, active_release_id=None)
            self.store.record_event(
                conn, candidate.task_id, TaskEventType.rollback_status,
                {
                    "release_id": candidate.id, "render_deploy_id": rollback.render_deploy_id, "render_status": LIVE,
                    "verified": True, "target_sha": rollback.target_sha,
                },
            )
            self._fail_rolled_back(conn, current, reason)
        return True

    def _fail_rolled_back(self, conn: Connection, candidate: ReleaseCandidate, reason: str) -> None:
        self._block_in(conn, candidate, reason, "verify_failed")
        self._settle(conn, candidate, S.failed, reason, FailureKind.verify_failed)
        for item in self._candidates(conn, "superseded_by = :id", {"id": candidate.id}):
            self._settle(conn, item, S.failed, f"Выпуск в составе {candidate.commit_sha[:7]} откатан.", FailureKind.verify_failed)

    def _restore_superseded(self, conn: Connection, candidate: ReleaseCandidate) -> None:
        conn.execute(
            "UPDATE release_candidates SET status = :pending, superseded_by = NULL, version = version + 1 "
            "WHERE superseded_by = :id AND status = :superseded",
            {"pending": R.pending.value, "superseded": R.superseded.value, "id": candidate.id},
        )

    def _next(self) -> bool:
        pending = [item for item in self._pending() if not item.blocked_reason]
        if not pending:
            return False
        try:
            base = self.reconcile_published()
        except QueueBusy:
            return False
        except BaseUnavailable as error:
            rollbacks = [item for item in pending if item.kind == ReleaseKind.rollback]
            return self._halt((rollbacks or pending[::-1])[0], f"Деплой не начат: {error}")
        pending = [item for item in self._pending() if not item.blocked_reason]
        rollbacks = [item for item in pending if item.kind == ReleaseKind.rollback]
        if rollbacks:
            return self._deploy(rollbacks[0], base, [])
        releases = [item for item in pending if item.kind == ReleaseKind.release]
        if any(not item.suite_hash for item in releases):
            return False
        with self.store.db.connection() as conn:
            blocked = self.store.queue_state(conn)["release_blocked_by"]
            source = self.store.load_candidate(conn, blocked) if blocked else None
        if source is not None:
            fixes = fixes_of(source)
            releases = [
                item for item in releases if fixes and all(self.contains(item.commit_sha, sha) for sha in fixes)
            ]
        if not releases:
            return False
        chosen = releases[0]
        for item in releases[1:]:
            if self.contains(item.commit_sha, chosen.commit_sha):
                chosen = item
        older = [item for item in releases if item.id != chosen.id and self.contains(chosen.commit_sha, item.commit_sha)]
        return self._deploy(chosen, base, older)

    def _own_ids(self) -> set[str]:
        with self.store.db.connection() as conn:
            rows = self._candidates(conn, "deploy IS NOT NULL OR rollback IS NOT NULL", {})
        found = {item.deploy.render_deploy_id for item in rows if item.deploy}
        return found | {item.rollback.render_deploy_id for item in rows if item.rollback and item.rollback.render_deploy_id}

    def _wait_external(self, candidate: ReleaseCandidate, foreign: list[dict[str, Any]]) -> bool:
        now = self.clock()
        names = ", ".join(f"{deploy.get('id')} ({deploy.get('status')})" for deploy in foreign)
        with self.store.db.locked_transaction() as conn:
            since = self.store.queue_state(conn)["external_wait_since"]
            if since is None:
                self.store.set_queue(conn, external_wait_since=to_text(now))
                return False
            minutes = self.settings.external_deploy_wait_minutes
            if now - from_text(since) <= timedelta(minutes=minutes):
                return False
            self.store.set_queue(conn, external_wait_since=None)
            self._halt_in(
                conn, candidate, f"Render выполняет деплой вне офиса дольше {minutes:g} мин: {names}. Деплой не начат."
            )
        return False

    def _deploy(self, candidate: ReleaseCandidate, base: str, older: list[ReleaseCandidate]) -> bool:
        if candidate.expected_previous_sha != base:
            if candidate.kind == ReleaseKind.rollback:
                return self._revoke(
                    candidate, f"Опубликована версия {base[:7]}, а откат рассчитан от {candidate.expected_previous_sha[:7]}."
                )
            return self._rebase(candidate, base)
        deploys = self.render.list_deploys()
        own = self._own_ids()
        foreign = [deploy for deploy in deploys if not is_terminal(deploy.get("status")) and deploy.get("id") not in own]
        if foreign:
            return self._wait_external(candidate, foreign)
        with self.store.db.locked_transaction() as conn:
            if self.store.queue_state(conn)["external_wait_since"]:
                self.store.set_queue(conn, external_wait_since=None)
        target = None
        if candidate.kind == ReleaseKind.rollback:
            target = self._target_deploy(candidate.commit_sha, deploys)
            if target is None:
                return self._halt(candidate, f"Деплой версии {candidate.commit_sha[:7]} для отката не найден в Render.")
        else:
            problems = self.composition(base, candidate.commit_sha)
            if problems:
                return self._halt(candidate, f"Состав выпуска не подтверждён: {'; '.join(problems)}")
        with self.store.db.locked_transaction() as conn:
            current = self.store.load_candidate(conn, candidate.id)
            if (
                self.busy_in(conn)
                or current.status != R.pending
                or current.blocked_reason
                or current.version != candidate.version
            ):
                return False
            for item in older:
                if self.store.load_candidate(conn, item.id).status == R.pending:
                    self.store.update_candidate(conn, item.id, status=R.superseded.value, superseded_by=candidate.id)
            self.store.update_candidate(conn, candidate.id, status=R.deploying.value)
            self.store.set_queue(conn, active_release_id=candidate.id)
            task = self.store.load_task(conn, candidate.task_id)
            if candidate.kind == ReleaseKind.release and candidate.origin == ReleaseOrigin.task and task.status == S.awaiting_release:
                self.store.transition(conn, task.id, S.deploying, expected_from=S.awaiting_release)
            kind = ROLLBACK if candidate.kind == ReleaseKind.rollback else DEPLOY
            operation, allowed = self.ops.begin_in(conn, kind, f"{kind}:{candidate.id}", candidate.task_id, candidate.id)
        if not allowed:
            return False
        if candidate.kind == ReleaseKind.rollback:
            return self._call(candidate, operation, "deploy", lambda: self.render.rollback(target))
        return self._call(candidate, operation, "deploy", lambda: self.render.deploy(candidate.commit_sha))

    def _rebase(self, candidate: ReleaseCandidate, base: str) -> bool:
        reason = (
            f"Опубликованная версия сменилась: {base[:7]} вместо {candidate.expected_previous_sha[:7]}. "
            "Создан новый кандидат того же commit: проверки итогового commit выполняются заново."
        )
        with self.store.db.locked_transaction() as conn:
            if self.store.load_candidate(conn, candidate.id).status != R.pending:
                return False
            self.store.update_candidate(conn, candidate.id, status=R.revoked.value, blocked_reason=reason)
            self.store.create_candidate(
                conn,
                task_id=candidate.task_id,
                kind=candidate.kind,
                commit_sha=candidate.commit_sha,
                expected_previous_sha=base,
                spec_hash=candidate.spec_hash,
                suite_hash="",
                ci_run_ids=[],
                pr_url=candidate.pr_url,
                pr_review_id=candidate.pr_review_id,
                origin=candidate.origin,
                origin_decision_id=candidate.origin_decision_id,
            )
        return True

    def _revoke(self, candidate: ReleaseCandidate, reason: str) -> bool:
        with self.store.db.locked_transaction() as conn:
            if self.store.load_candidate(conn, candidate.id).status != R.pending:
                return False
            self.store.update_candidate(conn, candidate.id, status=R.revoked.value, blocked_reason=reason)
            self._settle(conn, candidate, S.needs_attention, f"Откат отозван: {reason}")
        return True

    def composition(self, base: str, head: str) -> list[str]:
        data = self.github.compare(base, head)
        if data.get("status") != "ahead":
            return [f"{head[:7]} не является потомком основания {base[:7]}"]
        commits = data.get("commits") or []
        if int(data.get("total_commits", len(commits))) > len(commits):
            return [f"диапазон {base[:7]}..{head[:7]} больше {len(commits)} commit, состав не проверен"]
        parents = {item["sha"]: [parent["sha"] for parent in item.get("parents") or []] for item in commits}
        line: list[str] = []
        sha = head
        while sha != base:
            if not parents.get(sha):
                return [f"первая линия {base[:7]}..{head[:7]} не прослеживается"]
            line.append(sha)
            sha = parents[sha][0]
        position = {sha: index for index, sha in enumerate(line)}
        problems = []
        pushes: list[dict[str, Any]] | None = None
        for sha in line:
            merged, problem = self._merged_pr(sha)
            if not merged:
                if pushes is None:
                    pushes = list(self.github.push_activity(ACTIVITY_PAGES))
                problem = self._pushed(sha, pushes, position, base, head)
            if problem:
                problems.append(f"{sha[:7]}: {problem}")
        return problems

    def _merged_pr(self, sha: str) -> tuple[bool, str | None]:
        pulls = [pr for pr in self.github.commit_pulls(sha) if pr.get("merged_at") and pr.get("merge_commit_sha") == sha]
        if not pulls:
            return False, None
        number = pulls[0]["number"]
        login = (self.github.get_pr(number).get("merged_by") or {}).get("login")
        if login == f"{self.settings.github_app_slug}[bot]":
            if self.office_merge(sha):
                return True, None
            return True, f"PR #{number} слит App, но офис не допускал его к слиянию"
        if login in self.settings.trusted_github_logins:
            return True, None
        return True, f"PR #{number} слит {login or 'неизвестным'}: логин не входит в OFFICE_TRUSTED_GITHUB_LOGINS"

    def office_merge(self, sha: str) -> bool:
        with self.store.db.connection() as conn:
            merge = conn.fetchone(
                "SELECT * FROM operations WHERE kind = 'merge_pr' AND status = 'succeeded' AND external_ref = :sha",
                {"sha": sha},
            )
            if merge is None:
                return False
            carrier, head = merge["key"].removeprefix("merge_pr:").rsplit(":", 1)
            pushed = conn.fetchone(
                "SELECT id FROM operations WHERE key = :key AND status = 'succeeded'", {"key": f"push:{carrier}:{head}"}
            )
            review = self.store.load_review(conn, merge["task_id"], head)
            rows = conn.fetchall(
                "SELECT payload FROM task_events WHERE task_id = :id AND type = 'checks_status' ORDER BY seq DESC",
                {"id": merge["task_id"]},
            )
        summaries = [payload for payload in (json.loads(row["payload"]) for row in rows) if payload.get("head_sha") == head]
        checks = summaries[0].get("checks") if summaries else []
        passed = bool(checks) and all(
            check.get("status") == "completed" and check.get("conclusion") == "success" for check in checks
        )
        return pushed is not None and review is not None and review.verdict == "approve" and passed

    def _pushed(
        self, sha: str, pushes: list[dict[str, Any]], position: dict[str, int], base: str, head: str
    ) -> str | None:
        for item in pushes:
            if (item.get("actor") or {}).get("login") not in self.settings.trusted_github_logins:
                continue
            try:
                if self._in_push(item, sha, position, base, head):
                    return None
            except GitHubError:
                continue
        return "нет слитого PR и push доверенного логина в main"

    def _in_push(self, item: dict[str, Any], sha: str, position: dict[str, int], base: str, head: str) -> bool:
        after, before = item.get("after"), item.get("before")
        if after == sha:
            return True
        if not isinstance(after, str) or not isinstance(before, str):
            return False
        newer = position[after] < position[sha] if after in position else self.contains(after, head)
        older = position[before] > position[sha] if before in position else self.contains(base, before)
        return newer and older

    def decide(self, participant: Participant, release_id: str, request: ReleaseDecisionRequest) -> Decision:
        require_admin(participant)
        observed = None
        if request.decision == ReleaseDecision.resume_releases:
            if self.render is None:
                raise not_allowed("Выпуск не настроен: RENDER_API_KEY и RENDER_SERVICE_ID не заданы.")
            with self.store.db.connection() as conn:
                replayed = conn.fetchone(
                    "SELECT id FROM decisions WHERE participant_id = :participant AND idempotency_key = :key",
                    {"participant": participant.id, "key": request.idempotency_key},
                )
                active = self.store.queue_state(conn)["active_release_id"]
            if request.fix_sha is None and replayed is None and active == release_id:
                live = self.observe()
                try:
                    observed = (live, self.contained(self.store.get_candidate(release_id)))
                except (GitHubError, GitHubUnavailable):
                    raise not_allowed("GitHub недоступен: состав выпуска не сверен, повторите решение.") from None
        return decide_release(self.store, participant, release_id, request, partial(self.resume_in, observed))

    def observe(self) -> str:
        if any(operation.status == OpStatus.started for operation in self.ops.open_kinds(QUEUE_OPS)):
            raise not_allowed("Запрос деплоя или отката ещё сверяется: повторите позже.")
        try:
            deploys = self.render.list_deploys()
        except (RenderError, RenderUnavailable):
            raise not_allowed("Render недоступен: сверка не выполнена.") from None
        running = [deploy for deploy in deploys if not is_terminal(deploy.get("status"))]
        if running:
            raise not_allowed(
                f"Деплой Render {running[0].get('id')} ещё выполняется ({running[0].get('status')}): сверка невозможна."
            )
        try:
            version = self.service.version()
        except BaseUnavailable as error:
            raise not_allowed(f"Сверка не выполнена: {error}") from None
        live = max((deploy for deploy in deploys if deploy.get("status") == LIVE), key=created_of, default=None)
        sha = commit_of(live)
        if not sha or not sha.startswith(version):
            raise not_allowed(f"Версия сервиса {version} не совпадает с live-деплоем Render: сверка невозможна.")
        return sha

    def resume_in(
        self, observed: tuple[str, frozenset[str]] | None, conn: Connection, candidate: ReleaseCandidate,
        decision: Decision, request: ReleaseDecisionRequest,
    ) -> None:
        state = self.store.queue_state(conn)
        if request.fix_sha is not None:
            if state["release_blocked_by"] != candidate.id:
                raise not_allowed("fix_sha указывается только для выпуска, после отката которого выпуск заблокирован.")
            revert = candidate.revert
            if revert is not None and revert.status == "merged" and revert.fix_sha == request.fix_sha:
                raise not_allowed(
                    f"Исправление {request.fix_sha[:7]} уже принято, кандидат исправления создан: "
                    "дождитесь его выпуска или укажите другой commit."
                )
            resumed = new_revert("resume", request.fix_sha, decision.decision_id)
            if revert is not None:
                targets = [
                    target if target.fixed_by else target.model_copy(update={"attempt": revert.attempt})
                    for target in targets_of(candidate)
                ]
                resumed = resumed.model_copy(update={"attempt": revert.attempt, "tree": revert.tree, "targets": targets})
            self.store.update_candidate(conn, candidate.id, revert=resumed)
            return
        if state["release_blocked_by"] == candidate.id:
            self._retry_revert_in(conn, candidate, decision)
            return
        if not candidate.blocked_reason:
            raise not_allowed("Выпуск не остановлен: возобновлять нечего.")
        if candidate.id == state["active_release_id"]:
            if observed is None:
                raise not_allowed("Сверка не выполнена: повторите решение.")
            self._resolve_in(conn, candidate, observed[0], observed[1])
            return
        if candidate.status != R.pending:
            raise not_allowed("Возобновить можно только остановленный выпуск в очереди или активный выпуск.")
        self.store.update_candidate(conn, candidate.id, blocked_reason=None)
        task = self.store.load_task(conn, candidate.task_id)
        if candidate.origin == ReleaseOrigin.task and candidate.kind == ReleaseKind.release and task.status == S.needs_attention:
            self.store.transition(conn, task.id, S.awaiting_release, expected_from=S.needs_attention)

    def _retry_revert_in(self, conn: Connection, candidate: ReleaseCandidate, decision: Decision) -> None:
        revert = candidate.revert
        if revert is None or revert.status != "needs_attention":
            raise not_allowed(
                "Блокировка после отката снимается публикацией исправления: укажите fix_sha. Без fix_sha решение "
                "повторяет revert-PR, только когда он требует внимания."
            )
        if revert.source == "auto" and revert.fix_sha is not None:
            raise not_allowed("Revert-PR уже слит в main: укажите fix_sha исправления.")
        row = conn.fetchone(
            "SELECT key FROM operations WHERE release_id = :id AND status IN ('started', 'unknown') LIMIT 1",
            {"id": candidate.id},
        )
        if row is not None:
            raise not_allowed(f"Исход операции {row['key']} не установлен: повтор revert-PR недоступен, укажите fix_sha.")
        self.store.update_candidate(
            conn, candidate.id,
            revert=revert.model_copy(
                update={
                    "status": "pending", "source": "auto", "attempt": revert.attempt + 1, "fix_sha": None,
                    "decision_id": decision.decision_id, "candidate_id": None, "reason": None,
                }
            ),
        )

    def _resolve_in(self, conn: Connection, candidate: ReleaseCandidate, live: str, contained: frozenset[str]) -> None:
        rows = conn.fetchall(
            "SELECT * FROM operations WHERE release_id = :id AND kind IN ('deploy', 'rollback')", {"id": candidate.id}
        )
        rolled = candidate.kind == ReleaseKind.release and any(row["kind"] == ROLLBACK for row in rows)
        published = candidate.commit_sha == live
        for row in rows:
            if row["status"] in (OpStatus.started, OpStatus.unknown):
                conn.execute(
                    "UPDATE operations SET status = :status, detail = :detail WHERE id = :id",
                    {
                        "status": (OpStatus.succeeded if published else OpStatus.failed).value,
                        "detail": f"resume_releases: опубликована {live[:7]}", "id": row["id"],
                    },
                )
        if published:
            self._publish_in(conn, candidate, contained)
            return
        reason = f"Сверка «Возобновить выпуск»: опубликована {live[:7]}, выпуск {candidate.commit_sha[:7]} не опубликован."
        self.store.update_candidate(conn, candidate.id, status=R.failed.value)
        self.store.set_queue(conn, active_release_id=None)
        released = self._candidates(conn, "status = :status AND commit_sha = :sha", {"status": R.released.value, "sha": live})
        self._set_published(conn, released[-1] if released else None, live)
        if rolled:
            self.store.update_candidate(conn, candidate.id, blocked_reason=reason)
            self._fail_rolled_back(conn, candidate, reason)
            return
        self._restore_superseded(conn, candidate)
        self._settle(conn, candidate, S.failed, reason, FailureKind.deploy_failed)
