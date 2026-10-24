from app.errors import ErrorCode, OfficeError
from app.schemas import (
    FailureKind,
    Participant,
    ReleaseCandidate,
    ReleaseDecision,
    ReleaseKind,
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

FINAL_FAILURES: dict[FailureKind, str] = {
    FailureKind.checks_failed: "Обязательные проверки PR не пройдены, PR закрыт. Повтор недоступен: поставьте новую задачу.",
    FailureKind.pr_rejected: "Агент ревью отклонил PR. Повтор недоступен: поставьте новую задачу.",
    FailureKind.deploy_failed: "Деплой выпуска не выполнен. Повтор недоступен: изменение уже в main и войдёт в следующий выпуск.",
    FailureKind.verify_failed: "Выпуск откатан после проверки. Повтор недоступен: поставьте новую задачу.",
}


def require_task_owner(participant: Participant, task: Task, decision: TaskDecision | None = None) -> None:
    integration = task.status == TaskStatus.needs_attention and task.integration_started
    if decision == TaskDecision.retry and integration and participant.role == Role.admin:
        return
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
            "Откат и возобновление выпуска доступны только администратору.",
        )


def not_allowed(message: str) -> OfficeError:
    return OfficeError(409, ErrorCode.decision_not_allowed, message)


MERGED = "PR задачи уже слит в main или слияние выполняется: отмена недоступна."


def check_task_decision(task: Task, decision: TaskDecision) -> None:
    integration = task.status == TaskStatus.needs_attention and task.integration_started
    if task.status not in TASK_DECISION_STATUSES[decision]:
        if decision == TaskDecision.retry:
            raise not_allowed(
                "Повтор доступен только для задач в статусах «Ошибка» и «Требует внимания»."
            )
        raise not_allowed("Решение недопустимо в текущем статусе задачи.")
    if decision == TaskDecision.approve_spec and task.spec is None:
        raise not_allowed("Требования ещё не подготовлены.")
    if decision == TaskDecision.cancel and (task.merge_sha or task.merge_pending):
        raise not_allowed(MERGED)
    if decision == TaskDecision.retry and task.status == TaskStatus.failed and task.failure_kind in FINAL_FAILURES:
        raise not_allowed(FINAL_FAILURES[task.failure_kind])
    if decision == TaskDecision.retry and not integration and task.usage.model_calls >= task.usage.model_calls_limit:
        raise not_allowed("Бюджет обращений к моделям исчерпан. Повтор недоступен.")


def check_release_decision(candidate: ReleaseCandidate, decision: ReleaseDecision, fix_sha: str | None = None) -> None:
    if decision == ReleaseDecision.rollback and fix_sha is not None:
        raise not_allowed("fix_sha указывается только в решении «Возобновить выпуск».")
    if decision == ReleaseDecision.rollback and not (
        candidate.status == ReleaseStatus.released and candidate.is_published and candidate.kind == ReleaseKind.release
    ):
        raise not_allowed("Откатить можно только опубликованный сейчас выпуск.")
