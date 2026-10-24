import hashlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.db import Connection, from_text, to_text, utcnow
from app.errors import ErrorCode, OfficeError, not_found
from app.permissions import (
    check_release_decision,
    check_task_decision,
    not_allowed,
    require_admin,
    require_task_owner,
)
from app.schemas import (
    Decision,
    Participant,
    ReleaseCandidate,
    ReleaseDecision,
    ReleaseDecisionRequest,
    ReleaseKind,
    ReleaseOrigin,
    ReleaseStatus,
    TaskDecision,
    TaskDecisionRequest,
    TaskEventType,
    TaskStatus,
)
from app.tasks import SIGNAL_DECISION_RECORDED, TaskStore, new_id

TASK_OPERATION = "task_decision"
RELEASE_OPERATION = "release_decision"

TASK_TARGETS = {
    TaskDecision.approve_spec: TaskStatus.developing,
    TaskDecision.cancel: TaskStatus.cancelled,
    TaskDecision.retry: TaskStatus.queued,
}


def request_hash(body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decision_from_row(row: dict[str, Any]) -> Decision:
    is_task = row["operation"] == TASK_OPERATION
    return Decision(
        decision_id=row["id"],
        decision=row["decision"],
        task_id=row["object_id"] if is_task else None,
        release_id=None if is_task else row["object_id"],
        participant_id=row["participant_id"],
        expected_version=row["expected_version"],
        idempotency_key=row["idempotency_key"],
        recorded_at=from_text(row["recorded_at"]),
    )


def replay(
    conn: Connection, participant_id: str, key: str, operation: str, object_id: str, body_hash: str
) -> Decision | None:
    row = conn.fetchone(
        "SELECT * FROM decisions WHERE participant_id = :participant AND idempotency_key = :key",
        {"participant": participant_id, "key": key},
    )
    if row is None:
        return None
    if (row["operation"], row["object_id"], row["request_hash"]) != (operation, object_id, body_hash):
        raise OfficeError(
            409,
            ErrorCode.idempotency_conflict,
            "Ключ идемпотентности уже использован с другим запросом.",
        )
    return decision_from_row(row)


def insert_decision(
    conn: Connection,
    *,
    participant_id: str,
    operation: str,
    object_id: str,
    body_hash: str,
    decision: str,
    expected_version: int,
    key: str,
) -> Decision:
    row = {
        "id": new_id("dec"),
        "participant_id": participant_id,
        "idempotency_key": key,
        "operation": operation,
        "object_id": object_id,
        "request_hash": body_hash,
        "decision": decision,
        "expected_version": expected_version,
        "recorded_at": to_text(utcnow()),
    }
    conn.execute(
        "INSERT INTO decisions (id, participant_id, idempotency_key, operation, object_id, request_hash, "
        "decision, expected_version, recorded_at) VALUES (:id, :participant_id, :idempotency_key, "
        ":operation, :object_id, :request_hash, :decision, :expected_version, :recorded_at)",
        row,
    )
    return decision_from_row(row)


def version_mismatch(message: str) -> OfficeError:
    return OfficeError(409, ErrorCode.version_mismatch, message)


def decide_task(
    store: TaskStore, participant: Participant, task_id: str, request: TaskDecisionRequest
) -> Decision:
    body_hash = request_hash(request)
    with store.db.locked_transaction() as conn:
        task = store.load_task(conn, task_id)
        if task is None:
            raise not_found("Задача не найдена.")
        require_task_owner(participant, task, request.decision)
        previous = replay(conn, participant.id, request.idempotency_key, TASK_OPERATION, task_id, body_hash)
        if previous is not None:
            return previous
        if task.version != request.expected_version:
            raise version_mismatch("Задача изменилась. Обновите карточку и примите решение заново.")
        integration = task.status == TaskStatus.needs_attention and task.integration_started
        check_task_decision(task, request.decision)
        if (
            request.decision == TaskDecision.retry
            and integration
            and not task.merge_sha
            and not store.has_review(conn, task_id, task.head_sha)
            and store.pr_review_calls_used(conn, task_id) >= store.pr_review_calls
        ):
            raise not_allowed("Предел обращений агента ревью PR исчерпан. Повтор недоступен.")
        if request.decision == TaskDecision.retry and integration and task.merge_sha:
            released = conn.fetchone(
                "SELECT id FROM release_candidates WHERE task_id = :id AND kind = :kind AND status <> :revoked",
                {"id": task_id, "kind": ReleaseKind.release.value, "revoked": ReleaseStatus.revoked.value},
            )
            if released is not None:
                raise not_allowed("Кандидат выпуска уже создан: выпуск продолжает администратор решением «Возобновить выпуск».")
        decision = insert_decision(
            conn,
            participant_id=participant.id,
            operation=TASK_OPERATION,
            object_id=task_id,
            body_hash=body_hash,
            decision=request.decision.value,
            expected_version=request.expected_version,
            key=request.idempotency_key,
        )
        store.record_event(
            conn,
            task_id,
            TaskEventType.decision_recorded,
            {
                "decision": decision.decision.value,
                "decision_id": decision.decision_id,
                "participant_id": participant.id,
            },
        )
        target = TASK_TARGETS[request.decision]
        if request.decision == TaskDecision.retry and integration:
            target = TaskStatus.awaiting_integration
        store.transition(conn, task_id, target, expected_from=task.status)
        store.signal(conn, SIGNAL_DECISION_RECORDED, task_id)
    if request.decision == TaskDecision.retry:
        store.promote_queue()
    return decision


def decide_release(
    store: TaskStore,
    participant: Participant,
    release_id: str,
    request: ReleaseDecisionRequest,
    resume: Callable[[Connection, ReleaseCandidate, Decision, ReleaseDecisionRequest], None] | None = None,
) -> Decision:
    body_hash = request_hash(request)
    with store.db.locked_transaction() as conn:
        candidate = store.load_candidate(conn, release_id)
        if candidate is None:
            raise not_found("Кандидат выпуска не найден.")
        require_admin(participant)
        previous = replay(
            conn, participant.id, request.idempotency_key, RELEASE_OPERATION, release_id, body_hash
        )
        if previous is not None:
            return previous
        if candidate.version != request.expected_version:
            raise version_mismatch(
                "Кандидат выпуска изменился. Проверьте новую версию и примите решение заново."
            )
        check_release_decision(candidate, request.decision, request.fix_sha)
        if request.decision == ReleaseDecision.resume_releases and resume is None:
            raise not_allowed("Выпуск не настроен: исполнитель выпуска и Render не подключены.")
        if request.decision == ReleaseDecision.rollback:
            active = conn.fetchone(
                "SELECT id FROM release_candidates WHERE kind = :kind AND commit_sha = :target "
                "AND expected_previous_sha = :current AND status IN (:pending, :deploying, :verifying)",
                {
                    "kind": ReleaseKind.rollback.value,
                    "target": candidate.expected_previous_sha,
                    "current": candidate.commit_sha,
                    "pending": ReleaseStatus.pending.value,
                    "deploying": ReleaseStatus.deploying.value,
                    "verifying": ReleaseStatus.verifying.value,
                },
            )
            if active is not None:
                raise not_allowed("Откат этого выпуска уже ждёт в очереди.")
        decision = insert_decision(
            conn,
            participant_id=participant.id,
            operation=RELEASE_OPERATION,
            object_id=release_id,
            body_hash=body_hash,
            decision=request.decision.value,
            expected_version=request.expected_version,
            key=request.idempotency_key,
        )
        store.record_event(
            conn,
            candidate.task_id,
            TaskEventType.decision_recorded,
            {
                "decision": decision.decision.value,
                "decision_id": decision.decision_id,
                "participant_id": participant.id,
            },
        )
        store.signal(conn, SIGNAL_DECISION_RECORDED, candidate.task_id)
        if request.decision == ReleaseDecision.resume_releases:
            resume(conn, candidate, decision, request)
        else:
            source = conn.fetchone(
                "SELECT spec_hash, suite_hash, ci_run_ids FROM release_candidates "
                "WHERE commit_sha = :target AND id <> :id ORDER BY created_at DESC LIMIT 1",
                {"target": candidate.expected_previous_sha, "id": release_id},
            )
            store.create_candidate(
                conn,
                task_id=candidate.task_id,
                kind=ReleaseKind.rollback,
                commit_sha=candidate.expected_previous_sha,
                expected_previous_sha=candidate.commit_sha,
                spec_hash=source["spec_hash"] if source else candidate.spec_hash,
                suite_hash=source["suite_hash"] if source else candidate.suite_hash,
                ci_run_ids=json.loads(source["ci_run_ids"]) if source else [],
                pr_url=None,
                origin=ReleaseOrigin.manual_rollback,
                origin_decision_id=decision.decision_id,
            )
    return decision
