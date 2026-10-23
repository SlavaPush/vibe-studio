import pytest
from fastapi.testclient import TestClient

from app import fixtures
from app.main import app
from app.schemas import TaskStatus, count_slots

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


def test_slots_route_counts_fixture_tasks():
    response = TestClient(app).get("/api/slots")
    assert response.status_code == 200
    assert response.json() == {"used": 2, "total": 3}
