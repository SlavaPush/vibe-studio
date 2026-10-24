"""Тестовые данные этапа P1. Заменяются хранилищем офиса на этапе P4."""

from datetime import UTC, datetime

from app.schemas import (
    DeployResult,
    ModelProfile,
    Participant,
    ReleaseCandidate,
    ReleaseKind,
    ReleaseStatus,
    Role,
    Spec,
    Task,
    TaskStatus,
    Usage,
)

PUBLISHED_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _at(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


ADMIN = Participant(
    id="prt_admin",
    display_name="Вячеслав",
    role=Role.admin,
    expires_at=_at(9, day=24),
)

PARTICIPANT = Participant(
    id="prt_maria",
    display_name="Мария",
    role=Role.participant,
    expires_at=_at(9, day=24),
)

TASKS = [
    Task(
        id="tsk_103",
        title="Перевести backend vibe-chat на другую базу данных",
        text="Перевести backend vibe-chat на другую базу данных и обновить миграции.",
        status=TaskStatus.rejected,
        profile=ModelProfile.cloud,
        owner_id="prt_ivan",
        owner_name="Иван",
        version=2,
        created_at=_at(11),
        updated_at=_at(11, 1),
        rejection_reason="Задача меняет backend и схему БД. Офис принимает только изменения интерфейса vibe-chat.",
        spec=None,
        branch=None,
        base_sha=None,
        pr_url=None,
        changed_files=[],
        fix_cycles=0,
        usage=Usage(model_calls=1, model_calls_limit=30, input_tokens=640, output_tokens=85),
    ),
    Task(
        id="tsk_101",
        title="Кнопка копирования ответа ассистента",
        text="Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена.",
        status=TaskStatus.developing,
        profile=ModelProfile.hybrid,
        owner_id=PARTICIPANT.id,
        owner_name=PARTICIPANT.display_name,
        version=5,
        created_at=_at(9),
        updated_at=_at(10, 40),
        rejection_reason=None,
        spec=Spec(
            version=1,
            spec_hash="sha256:4b1d9e07c2a85f36",
            summary="Под каждым завершённым ответом ассистента появляется кнопка «Копировать».",
            acceptance_criteria=["Кнопка доступна с клавиатуры и имеет подпись для экранного диктора."],
            sources=[],
        ),
        branch="office/tsk_101",
        base_sha=PUBLISHED_SHA,
        pr_url=None,
        changed_files=["web/src/components/chat/MessageActions.tsx"],
        fix_cycles=0,
        usage=Usage(model_calls=9, model_calls_limit=30, input_tokens=48210, output_tokens=9120),
    ),
    Task(
        id="tsk_102",
        title="Переключатель тёмной темы в настройках",
        text="Добавить в настройки чата переключатель светлой и тёмной темы с сохранением выбора.",
        status=TaskStatus.awaiting_release_approval,
        profile=ModelProfile.cloud,
        owner_id=ADMIN.id,
        owner_name=ADMIN.display_name,
        version=14,
        created_at=_at(7, 30),
        updated_at=_at(10, 50),
        rejection_reason=None,
        spec=Spec(
            version=2,
            spec_hash="sha256:9c1e4b7a2d3f5e60",
            summary="В настройках чата есть переключатель «Тёмная тема».",
            acceptance_criteria=["Выбранная тема сохраняется после перезагрузки страницы."],
            sources=[],
        ),
        branch="office/tsk_102",
        base_sha=PUBLISHED_SHA,
        pr_url="https://github.com/SlavaPush/vibe-studio/pull/12",
        changed_files=["web/src/components/settings/ThemeSwitch.tsx"],
        fix_cycles=1,
        usage=Usage(model_calls=21, model_calls_limit=30, input_tokens=132400, output_tokens=24310),
    ),
]

RELEASES = [
    ReleaseCandidate(
        id="rel_7",
        task_id="tsk_102",
        kind=ReleaseKind.release,
        status=ReleaseStatus.pending,
        version=1,
        commit_sha="c3d4e5f6a7b8091a2b3c4d5e6f708192a3b4c5d6",
        expected_previous_sha=PUBLISHED_SHA,
        spec_hash="sha256:9c1e4b7a2d3f5e60",
        suite_hash="sha256:71c0d2e93a4b5f18",
        ci_run_ids=["18342210771"],
        pr_url="https://github.com/SlavaPush/vibe-studio/pull/12",
        created_at=_at(10, 49),
        approved_by=None,
        approved_at=None,
        is_published=False,
        deploy=None,
    ),
    ReleaseCandidate(
        id="rel_6",
        task_id="tsk_090",
        kind=ReleaseKind.release,
        status=ReleaseStatus.released,
        version=4,
        commit_sha=PUBLISHED_SHA,
        expected_previous_sha="5e0c1a7b9d2f4e6a8c0b1d3f5a7c9e1b3d5f7a90",
        spec_hash="sha256:2a7f0c3e9b1d4865",
        suite_hash="sha256:71c0d2e93a4b5f18",
        ci_run_ids=["18339906512"],
        pr_url="https://github.com/SlavaPush/vibe-studio/pull/9",
        created_at=_at(17, 40, day=22),
        approved_by=ADMIN.id,
        approved_at=_at(18, 2, day=22),
        is_published=True,
        deploy=DeployResult(
            render_deploy_id="dep-d1a2b3c4e5f6",
            render_status="live",
            started_at=_at(18, 3, day=22),
            finished_at=_at(18, 12, day=22),
            published_sha=PUBLISHED_SHA,
            error=None,
        ),
    ),
]
