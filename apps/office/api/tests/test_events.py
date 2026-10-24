import json
import threading
import time

import pytest

from app.schemas import TaskEvent, TaskEventType, TaskStatus


def read_messages(response, count, comments=False):
    messages, current = [], {}
    for line in response.iter_lines():
        if line == "":
            if current and (comments or "comment" not in current):
                messages.append(current)
                if len(messages) == count:
                    return messages
            current = {}
            continue
        if line.startswith(":"):
            current = {"comment": line[1:].strip()}
            continue
        field, _, value = line.partition(": ")
        current[field] = value
    return messages


def test_version_and_seq_have_no_gaps(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    office.store.promote_queue()
    task = office.store.get_task(task_id)
    for key in ("cancel-1",):
        client.post(
            f"/api/tasks/{task_id}/decisions",
            json={"decision": "cancel", "expected_version": task.version, "idempotency_key": key},
        )
    events = office.store.events_after(task_id, 0)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.type for event in events] == [
        TaskEventType.task_created,
        TaskEventType.status_changed,
        TaskEventType.decision_recorded,
        TaskEventType.status_changed,
    ]
    ids = [event.event_id for event in events]
    assert ids == sorted(ids)
    assert office.store.get_task(task_id).version == 3


def test_new_app_continues_event_ids_and_seq(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    last = office.store.events_after(task_id, 0)[-1]
    app = office.new_app()
    store = app.state.store
    events = store.events_after(task_id, 0)
    assert events[-1].seq == last.seq + 1
    assert events[-1].event_id > last.event_id
    assert store.get_task(task_id).status == TaskStatus.awaiting_spec_approval


def test_failed_transaction_leaves_no_event(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    before = office.store.get_task(task_id)
    with pytest.raises(RuntimeError):
        with office.store.db.locked_transaction() as conn:
            office.store.transition(conn, task_id, TaskStatus.cancelled, expected_from=TaskStatus.queued)
            raise RuntimeError("abort")
    after = office.store.get_task(task_id)
    assert after.version == before.version
    assert after.status == TaskStatus.queued
    assert len(office.store.events_after(task_id, 0)) == 1
    assert office.store.last_event_id() == 1


def test_task_stream_starts_from_first_event(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    office.store.promote_queue()
    with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        messages = read_messages(response, 2)
    assert [message["event"] for message in messages] == ["task_event", "task_event"]
    events = [TaskEvent.model_validate_json(message["data"]) for message in messages]
    assert [event.seq for event in events] == [1, 2]
    assert [message["id"] for message in messages] == [str(event.event_id) for event in events]


def test_task_stream_resumes_after_last_event_id(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    office.store.promote_queue()
    first = office.store.events_after(task_id, 0)[0]
    with client.stream("GET", f"/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(first.event_id)}) as response:
        messages = read_messages(response, 1)
    assert json.loads(messages[0]["data"])["seq"] == 2


def test_global_stream_receives_new_events(office):
    client, me = office.login()
    old_id = office.queued_task(me["id"])
    new_id = []

    def later():
        new_id.append(office.queued_task(me["id"]))

    timer = threading.Timer(0.3, later)
    with client.stream("GET", "/api/events") as response:
        timer.start()
        messages = read_messages(response, 1)
    timer.join()
    event = json.loads(messages[0]["data"])
    assert event["task_id"] == new_id[0] != old_id
    assert event["type"] == "task_created"


def test_global_stream_resumes_with_last_event_id(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    with client.stream("GET", "/api/events", headers={"Last-Event-ID": "0"}) as response:
        messages = read_messages(response, 1)
    assert json.loads(messages[0]["data"])["task_id"] == task_id


def test_stream_sends_ping(office):
    app = office.new_app(sse_ping_seconds=0.2)
    client, _ = office.login(app=app)
    with client.stream("GET", "/api/events") as response:
        started = time.monotonic()
        messages = read_messages(response, 2, comments=True)
        elapsed = time.monotonic() - started
    assert messages == [{"comment": "ping"}, {"comment": "ping"}]
    assert elapsed >= 0.15


@pytest.mark.parametrize("path", ["/api/events", "task"])
def test_stream_starts_with_ping(office, path):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    url = f"/api/tasks/{task_id}/events" if path == "task" else path
    with client.stream("GET", url) as response:
        started = time.monotonic()
        first = next(response.iter_text())
        elapsed = time.monotonic() - started
    assert first == ": ping\n\n"
    assert elapsed < 5


def test_stream_of_missing_task_is_json_error(office):
    client, _ = office.login()
    response = client.get("/api/tasks/tsk_missing/events")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
