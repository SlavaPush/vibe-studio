import threading

import pytest

from app import fixtures
from app.schemas import TaskStatus, count_slots
from app.tasks import SIGNAL_SLOT_ACQUIRED

SLOT_STATUSES = {
    "awaiting_spec_approval",
    "developing",
    "reviewing",
    "awaiting_integration",
    "awaiting_release_approval",
    "deploying",
}


@pytest.mark.parametrize("status", list(TaskStatus))
def test_count_slots_for_each_status(status):
    task = fixtures.TASKS[0].model_copy(update={"status": status})
    assert count_slots([task]) == (1 if status.value in SLOT_STATUSES else 0)


def test_count_slots_sums_tasks():
    tasks = [
        fixtures.TASKS[0].model_copy(update={"status": status})
        for status in (TaskStatus.queued, TaskStatus.developing, TaskStatus.deploying, TaskStatus.released)
    ]
    assert count_slots(tasks) == 2


def statuses(client):
    return {task["id"]: task["status"] for task in client.get("/api/tasks").json()}


def test_created_task_is_queued_then_takes_slot(office):
    client, _ = office.login()
    signals = []
    office.store.signal_handlers.append(lambda name, task_id: signals.append((name, task_id)))
    response = office.post_task(client)
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "queued"
    assert task["version"] == 1
    assert (task["spec"], task["branch"], task["base_sha"], task["pr_url"]) == (None, None, None, None)
    assert task["changed_files"] == []
    after = client.get(f"/api/tasks/{task['id']}").json()
    assert after["status"] == "awaiting_spec_approval"
    assert after["version"] == 2
    assert signals == [(SIGNAL_SLOT_ACQUIRED, task["id"])]
    assert client.get("/api/slots").json() == {"used": 1, "total": 3}


def test_fourth_task_waits_and_takes_released_slot(office):
    first, _ = office.login("Мария")
    second, _ = office.login("Иван")
    ids = [office.post_task(first).json()["id"] for _ in range(3)]
    fourth = office.post_task(second).json()["id"]
    current = statuses(first)
    assert [current[task_id] for task_id in ids] == ["awaiting_spec_approval"] * 3
    assert current[fourth] == "queued"
    assert first.get("/api/slots").json() == {"used": 3, "total": 3}

    task = first.get(f"/api/tasks/{ids[0]}").json()
    response = first.post(
        f"/api/tasks/{ids[0]}/decisions",
        json={"decision": "cancel", "expected_version": task["version"], "idempotency_key": "cancel-first"},
    )
    assert response.status_code == 200
    current = statuses(first)
    assert current[ids[0]] == "cancelled"
    assert current[fourth] == "awaiting_spec_approval"
    assert first.get("/api/slots").json() == {"used": 3, "total": 3}


def test_parallel_promotion_never_exceeds_three_slots(office):
    _, me = office.login()
    for _ in range(10):
        office.queued_task(me["id"])
    barrier = threading.Barrier(10)
    errors = []

    def promote():
        try:
            barrier.wait()
            office.store.promote_queue()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=promote) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert office.store.slots_used() == 3
    assert sum(1 for task in office.store.list_tasks() if task.status == TaskStatus.queued) == 7


def test_parallel_cancel_and_promote_keep_limit(office):
    client, me = office.login()
    ids = [office.queued_task(me["id"]) for _ in range(8)]
    office.store.promote_queue()
    barrier = threading.Barrier(6)

    def cancel(task_id):
        barrier.wait()
        task = office.store.get_task(task_id)
        client.post(
            f"/api/tasks/{task_id}/decisions",
            json={"decision": "cancel", "expected_version": task.version, "idempotency_key": f"cancel-{task_id}"},
        )

    def promote():
        barrier.wait()
        office.store.promote_queue()

    threads = [threading.Thread(target=cancel, args=(task_id,)) for task_id in ids[:3]]
    threads += [threading.Thread(target=promote) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert office.store.slots_used() == 3


def test_queued_task_takes_slot_in_new_app_instance(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    assert office.store.get_task(task_id).status == TaskStatus.queued
    app = office.new_app()
    assert app.state.store.get_task(task_id).status == TaskStatus.awaiting_spec_approval


def test_retry_takes_free_slot_without_other_requests(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    office.store.promote_queue()
    office.set_task(task_id, status="failed")
    task = client.get(f"/api/tasks/{task_id}").json()
    response = client.post(
        f"/api/tasks/{task_id}/decisions",
        json={"decision": "retry", "expected_version": task["version"], "idempotency_key": "retry-once"},
    )
    assert response.status_code == 200
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "awaiting_spec_approval"
