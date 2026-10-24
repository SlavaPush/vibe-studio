import json

import pytest

from app.schemas import TaskStatus
from app.tasks import SIGNAL_DECISION_RECORDED

pytestmark = pytest.mark.pg

SPEC = {
    "version": 1,
    "spec_hash": "sha256:spec",
    "summary": "Кнопка копирования ответа.",
    "acceptance_criteria": ["Кнопка есть у завершённых ответов."],
    "sources": [],
}


def decide(client, task_id, decision, version, key):
    return client.post(
        f"/api/tasks/{task_id}/decisions",
        json={"decision": decision, "expected_version": version, "idempotency_key": key},
    )


@pytest.fixture
def owner(office):
    client, me = office.login("Мария")
    task_id = office.queued_task(me["id"])
    office.store.promote_queue()
    return client, me, task_id


def version_of(office, task_id):
    return office.store.get_task(task_id).version


def test_foreign_task_is_forbidden_even_for_admin(office, owner):
    _, _, task_id = owner
    admin, _ = office.login("Админ", admin=True)
    stranger, _ = office.login("Иван")
    for client in (admin, stranger):
        response = decide(client, task_id, "cancel", version_of(office, task_id), "foreign-key-1")
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden_not_owner"
    assert office.store.get_task(task_id).status == TaskStatus.awaiting_spec_approval


def test_version_mismatch(office, owner):
    client, _, task_id = owner
    response = decide(client, task_id, "cancel", version_of(office, task_id) - 1, "stale-key-1")
    assert response.status_code == 409
    assert response.json()["code"] == "version_mismatch"


def test_approve_spec_requires_ready_spec(office, owner):
    client, _, task_id = owner
    response = decide(client, task_id, "approve_spec", version_of(office, task_id), "approve-early")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"

    office.set_task(task_id, spec=json.dumps(SPEC))
    signals = []
    office.store.signal_handlers.append(lambda name, task_id: signals.append((name, task_id)))
    version = version_of(office, task_id)
    response = decide(client, task_id, "approve_spec", version, "approve-ready")
    assert response.status_code == 200
    decision = response.json()
    assert decision["decision"] == "approve_spec"
    assert decision["task_id"] == task_id
    assert decision["release_id"] is None
    assert decision["expected_version"] == version
    task = office.store.get_task(task_id)
    assert task.status == TaskStatus.developing
    assert task.version == version + 1
    events = office.store.events_after(task_id, 0)
    assert [event.type for event in events][-2:] == ["decision_recorded", "status_changed"]
    assert events[-2].payload == {
        "decision": "approve_spec",
        "decision_id": decision["decision_id"],
        "participant_id": decision["participant_id"],
    }
    assert signals == [(SIGNAL_DECISION_RECORDED, task_id)]


def test_decision_not_allowed_in_status(office, owner):
    client, _, task_id = owner
    response = decide(client, task_id, "retry", version_of(office, task_id), "retry-early")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"


def test_retry_keeps_usage_and_fix_cycles(office, owner):
    client, _, task_id = owner
    office.set_task(task_id, status="needs_attention", model_calls=12, fix_cycles=2, input_tokens=500)
    response = decide(client, task_id, "retry", version_of(office, task_id), "retry-needs")
    assert response.status_code == 200
    task = office.store.get_task(task_id)
    assert task.status == TaskStatus.awaiting_spec_approval
    assert (task.usage.model_calls, task.fix_cycles, task.usage.input_tokens) == (12, 2, 500)


def test_retry_with_exhausted_budget_is_not_allowed(office, owner):
    client, _, task_id = owner
    office.set_task(task_id, status="failed", model_calls=30)
    response = decide(client, task_id, "retry", version_of(office, task_id), "retry-broke")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"


@pytest.mark.parametrize("kind", ["checks_failed", "pr_rejected"])
def test_retry_after_final_release_failure_is_not_allowed(office, owner, kind):
    client, _, task_id = owner
    office.set_task(task_id, status="failed", failure_kind=kind)
    response = decide(client, task_id, "retry", version_of(office, task_id), f"retry-{kind}")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"


def test_retry_without_release_operations_returns_to_queue(office, owner):
    client, _, task_id = owner
    office.set_task(task_id, status="needs_attention")
    assert decide(client, task_id, "retry", version_of(office, task_id), "retry-graph").status_code == 200
    assert office.store.get_task(task_id).status == TaskStatus.awaiting_spec_approval


def test_retry_with_pushed_head_returns_to_integration(office, owner):
    client, _, task_id = owner
    office.set_task(task_id, status="needs_attention", head_sha="a" * 40)
    assert decide(client, task_id, "retry", version_of(office, task_id), "retry-pushed").status_code == 200
    assert office.store.get_task(task_id).status == TaskStatus.awaiting_integration


def test_admin_retries_foreign_task_only_after_integration_started(office, owner):
    _, _, task_id = owner
    admin, _ = office.login("Админ", admin=True)
    stranger, _ = office.login("Иван")
    office.set_task(task_id, status="needs_attention")
    response = decide(admin, task_id, "retry", version_of(office, task_id), "admin-retry-graph")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_not_owner"
    office.set_task(task_id, head_sha="a" * 40)
    response = decide(stranger, task_id, "retry", version_of(office, task_id), "stranger-retry-pushed")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_not_owner"
    response = decide(admin, task_id, "cancel", version_of(office, task_id), "admin-cancel-pushed")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_not_owner"
    response = decide(admin, task_id, "retry", version_of(office, task_id), "admin-retry-pushed")
    assert response.status_code == 200
    assert office.store.get_task(task_id).status == TaskStatus.awaiting_integration


def test_cancel_awaiting_integration(office, owner):
    client, _, task_id = owner
    office.set_task(task_id, status="awaiting_integration", pr_number=3)
    assert decide(client, task_id, "cancel", version_of(office, task_id), "cancel-integration").status_code == 200
    assert office.store.get_task(task_id).status == TaskStatus.cancelled


def test_cancel_queued_task(office):
    client, me = office.login()
    for _ in range(3):
        office.queued_task(me["id"])
    office.store.promote_queue()
    task_id = office.queued_task(me["id"])
    response = decide(client, task_id, "cancel", version_of(office, task_id), "cancel-queued")
    assert response.status_code == 200
    assert office.store.get_task(task_id).status == TaskStatus.cancelled
    assert office.store.slots_used() == 3


def test_replay_returns_same_decision_without_reapplying(office, owner):
    client, _, task_id = owner
    version = version_of(office, task_id)
    first = decide(client, task_id, "cancel", version, "cancel-replay")
    second = decide(client, task_id, "cancel", version, "cancel-replay")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert version_of(office, task_id) == version + 1
    events = office.store.events_after(task_id, 0)
    assert [event.type for event in events].count("decision_recorded") == 1


def test_same_key_with_other_body_conflicts(office, owner):
    client, me, task_id = owner
    version = version_of(office, task_id)
    assert decide(client, task_id, "cancel", version, "shared-key-1").status_code == 200
    response = decide(client, task_id, "cancel", version + 1, "shared-key-1")
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"

    other_id = office.queued_task(me["id"])
    response = decide(client, other_id, "cancel", version_of(office, other_id), "shared-key-1")
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"


def test_replay_on_foreign_task_is_forbidden(office, owner):
    client, _, task_id = owner
    stranger, _ = office.login("Иван")
    version = version_of(office, task_id)
    assert decide(client, task_id, "cancel", version, "cancel-owner").status_code == 200
    response = decide(stranger, task_id, "cancel", version, "cancel-owner")
    assert response.status_code == 403


def test_missing_task_is_not_found(office):
    client, _ = office.login()
    response = decide(client, "tsk_missing", "cancel", 1, "missing-key")
    assert response.status_code == 404
