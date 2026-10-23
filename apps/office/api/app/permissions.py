from app.errors import ErrorCode, OfficeError
from app.schemas import (
    Participant,
    ReleaseCandidate,
    ReleaseDecision,
    ReleaseStatus,
    Role,
    Task,
    TaskDecision,
    TaskStatus,
)

TASK_DECISION_STATUSES: dict[TaskDecision, frozenset[TaskStatus]] = {
    TaskDecision.approve_spec: frozenset({TaskStatus.awaiting_spec_approval}),
    TaskDecision.cancel: frozenset(
        {
            TaskStatus.queued,
            TaskStatus.awaiting_spec_approval,
            TaskStatus.developing,
            TaskStatus.reviewing,
            TaskStatus.awaiting_integration,
            TaskStatus.needs_attention,
        }
    ),
    TaskDecision.retry: frozenset({TaskStatus.failed, TaskStatus.needs_attention}),
}


def require_task_owner(participant: Participant, task: Task) -> None:
    if task.owner_id != participant.id:
        raise OfficeError(
            403,
            ErrorCode.forbidden_not_owner,
            "Решения по задаче принимает только её владелец.",
        )


def require_admin(participant: Participant) -> None:
    if participant.role != Role.admin:
        raise OfficeError(
            403,
            ErrorCode.forbidden_role,
            "Подтверждение выпуска и откат доступны только администратору.",
        )


def not_allowed(message: str) -> OfficeError:
    return OfficeError(409, ErrorCode.decision_not_allowed, message)


def check_task_decision(task: Task, decision: TaskDecision) -> None:
    if task.status not in TASK_DECISION_STATUSES[decision]:
        if decision == TaskDecision.retry:
            raise not_allowed(
                "Повтор доступен только для задач в статусах «Ошибка» и «Требует внимания»."
            )
        raise not_allowed("Решение недопустимо в текущем статусе задачи.")
    if decision == TaskDecision.approve_spec and task.spec is None:
        raise not_allowed("Требования ещё не подготовлены.")
    if decision == TaskDecision.retry and task.usage.model_calls >= task.usage.model_calls_limit:
        raise not_allowed("Бюджет обращений к моделям исчерпан. Повтор недоступен.")


def check_release_decision(candidate: ReleaseCandidate, decision: ReleaseDecision) -> None:
    if decision == ReleaseDecision.approve_release and candidate.status != ReleaseStatus.pending:
        raise not_allowed("Подтвердить можно только кандидата в статусе «Ждёт подтверждения».")
    if decision == ReleaseDecision.rollback and not (
        candidate.status == ReleaseStatus.released and candidate.is_published
    ):
        raise not_allowed("Откатить можно только опубликованный сейчас выпуск.")
