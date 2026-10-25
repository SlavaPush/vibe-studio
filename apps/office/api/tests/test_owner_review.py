from datetime import timedelta

import pytest
from conftest import FakeSuite, pr_review_answer
from test_release_worker import drive, setup  # noqa: F401

from app.models_gateway import ProviderReply
from app.schemas import Contour, FailureKind, TaskEventType, TaskStatus

pytestmark = pytest.mark.pg

SCHEMA = "CREATE TABLE IF NOT EXISTS notes (\n    id uuid PRIMARY KEY\n);\n"
SUMMARY = "Добавлен эндпоинт заметок; схема БД дополнена таблицей notes. Рекомендация: одобрить."


def server_task(setup):
    setup.office.set_task(setup.task_id, change_class="backend_api")
    path = setup.workspace.worktree_path(setup.task_id)
    (path / "apps/chat/api/app").mkdir(parents=True, exist_ok=True)
    (path / "apps/chat/api/app/notes.py").write_text("NOTES = []\n", encoding="utf-8")
    (path / "apps/chat/api/app/schema.sql").write_text(SCHEMA, encoding="utf-8")
    setup.head = setup.workspace.commit(setup.task_id, "Заметки")
    assert setup.head is not None
    assert setup.task().contour == Contour.server
    setup.suite = FakeSuite(setup.store, setup.workspace)
    setup.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary=SUMMARY)
    worker = setup.worker()
    drive(worker, setup.task_id)
    setup.fake.add_pr_runs(setup.head, 1)
    return worker


def summary_writes(setup):
    return [
        request for request in setup.fake.requests
        if request.method == "PATCH" and request.url.path.endswith("/pulls/1") and b'"body"' in request.content
    ]


def test_server_pr_waits_for_owner_approval_of_current_head(setup):
    worker = server_task(setup)
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.awaiting_integration
    assert setup.fake.merges() == 0
    assert (task.owner_review.state, task.owner_review.head_sha) == ("requested", setup.head)
    assert SUMMARY in task.pr_review.owner_summary and "Факты офиса" in task.pr_review.owner_summary
    assert "схема БД" in task.pr_review.owner_summary.lower() or "Изменена схема БД" in task.pr_review.owner_summary
    body = setup.fake.prs[1]["body"]
    assert "## Сводка для владельца" in body and SUMMARY in body and "Откат Render не возвращает" in body
    assert "Изменены защитные тесты" not in body and "Удалены тесты" not in body
    assert [event.payload["head_sha"] for event in setup.events(TaskEventType.owner_review_requested)] == [setup.head]
    [call] = setup.gateway.role_calls("pr_reviewer")
    assert "backend_api" in call.user and "server" in call.user

    setup.clock.offset = timedelta(hours=5)
    setup.fake.review(1, "someone", "APPROVED", setup.head)
    setup.fake.review(1, "SlavaPush", "COMMENTED", setup.head)
    setup.fake.review(1, "SlavaPush", "APPROVED", "f" * 40)
    setup.fake.review(1, "SlavaPush", "DISMISSED", setup.head)
    worker = setup.worker()
    drive(worker, setup.task_id)
    assert setup.fake.merges() == 0
    assert setup.task().status == TaskStatus.awaiting_integration
    assert len(summary_writes(setup)) == 1

    setup.fake.review(1, "slavapush", "APPROVED", setup.head)
    drive(worker, setup.task_id)
    task = setup.task()
    assert setup.fake.merges() == 1
    assert task.merge_sha is not None
    assert (task.owner_review.state, task.owner_review.login) == ("approved", "slavapush")
    assert [event.payload["state"] for event in setup.events(TaskEventType.owner_review)] == ["approved"]
    assert len(summary_writes(setup)) == 1
    merge = [request for request in setup.fake.requests if request.method == "PUT"][0]
    assert setup.head.encode() in merge.content


def test_owner_summary_lists_changed_and_deleted_tests(setup):
    setup.office.set_task(setup.task_id, change_class="backend_api")
    path = setup.workspace.worktree_path(setup.task_id)
    (path / "apps/chat/api/tests/conftest.py").write_text("import pytest\n\nMODELS = []\n", encoding="utf-8")
    (path / "apps/chat/api/tests/test_new.py").write_text("def test_new():\n    assert True\n", encoding="utf-8")
    for old, new in (("App.test.tsx", "App.disabled.tsx"), ("test/setup.ts", "test/setup-moved.ts")):
        source = path / "apps/chat/web/src" / old
        (path / "apps/chat/web/src" / new).write_bytes(source.read_bytes())
        source.unlink()
    setup.head = setup.workspace.commit(setup.task_id, "Тесты")
    assert setup.task().contour == Contour.server
    setup.suite = FakeSuite(setup.store, setup.workspace)
    setup.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary=SUMMARY)
    worker = setup.worker()
    drive(worker, setup.task_id)
    setup.fake.add_pr_runs(setup.head, 1)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    body = setup.fake.prs[1]["body"]
    assert "- Изменены защитные тесты: apps/chat/api/tests/conftest.py." in body
    assert "- Удалены тесты: apps/chat/web/src/App.test.tsx, apps/chat/web/src/test/setup.ts." in body
    assert "test_new.py" not in body and "setup-moved.ts" not in body


def test_final_checks_timeout_applies_after_owner_approval(setup):
    worker = server_task(setup)
    drive(worker, setup.task_id)
    setup.fake.review(1, "SlavaPush", "APPROVED", setup.head)
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.merge_sha is not None and task.status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=29)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=31)
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert "итогового commit" in setup.events(TaskEventType.role_finished)[-1].payload["summary"]


def test_owner_request_changes_closes_pr_with_comment(setup):
    worker = server_task(setup)
    drive(worker, setup.task_id)
    setup.fake.review(1, "SlavaPush", "APPROVED", setup.head)
    setup.fake.review(1, "SlavaPush", "CHANGES_REQUESTED", setup.head, "Нужна пагинация")
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.owner_rejected)
    assert setup.fake.prs[1]["state"] == "closed"
    assert setup.fake.merges() == 0
    assert task.owner_review.comment == "Нужна пагинация"
    reason = setup.events(TaskEventType.role_finished)[-1].payload["summary"]
    assert "Владелец SlavaPush отклонил PR #1" in reason and "Нужна пагинация" in reason


def test_owner_reviews_are_read_from_every_page(setup):
    worker = server_task(setup)
    drive(worker, setup.task_id)
    for _ in range(100):
        setup.fake.review(1, "someone", "COMMENTED", setup.head)
    setup.fake.review(1, "SlavaPush", "APPROVED", setup.head)
    drive(worker, setup.task_id)
    assert setup.fake.merges() == 1


def test_server_review_without_owner_summary_needs_attention(setup):
    worker = server_task(setup)
    setup.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary="  ")
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert setup.fake.prs[1]["state"] == "open"
    assert task.pr_review is None
    assert "сводку для владельца" in setup.events(TaskEventType.role_finished)[-1].payload["summary"]


def test_agent_reject_closes_server_pr_without_owner(setup):
    worker = server_task(setup)
    setup.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer("reject", summary=SUMMARY)
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.pr_rejected)
    assert setup.events(TaskEventType.owner_review_requested) == []
    assert not any(request.url.path.endswith("/reviews") for request in setup.fake.requests)


def test_ui_pr_merges_without_owner_review(setup):
    worker = setup.worker()
    drive(worker, setup.task_id)
    setup.fake.add_pr_runs(setup.head, 1)
    drive(worker, setup.task_id)
    task = setup.task()
    assert setup.fake.merges() == 1
    assert task.contour == Contour.ui and task.owner_review is None
    assert task.pr_review.owner_summary is None
    assert not any(request.url.path.endswith("/reviews") for request in setup.fake.requests)


def test_invalid_owner_summary_reply_keeps_pr_open(setup):
    worker = server_task(setup)
    setup.gateway.handlers["pr_reviewer"] = lambda call: ProviderReply('{"verdict": "approve"}', 10, 2, True)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.fake.merges() == 0
