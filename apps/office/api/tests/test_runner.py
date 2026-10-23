import json
from datetime import timedelta

from conftest import UI_TASK, wait_for
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.runner import checkpoint_path
from app.schemas import ModelProfile, TaskEventType, TaskStatus
from app.tasks import IntakeOutcome, TaskStore

SPEC = {"version": 1, "spec_hash": "sha256:x", "summary": "s", "acceptance_criteria": ["c"], "sources": []}


def move(store, task_id, *statuses):
    with store.db.locked_transaction() as conn:
        for status in statuses:
            current = store.load_task(conn, task_id).status
            store.transition(conn, task_id, status, expected_from=current)


def retry_decision(store, task_id, owner_id):
    with store.db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO decisions (id, participant_id, idempotency_key, operation, object_id, request_hash, decision, "
            "expected_version, recorded_at) VALUES (:id, :owner, :id, 'task_decision', :task, 'x', 'retry', 1, "
            "'2026-10-24T00:00:00.000000Z')",
            {"id": f"dec_{task_id}", "owner": owner_id, "task": task_id},
        )
        store.record_event(
            conn, task_id, TaskEventType.decision_recorded,
            {"decision": "retry", "decision_id": "d", "participant_id": owner_id},
        )


def test_attempt_clock_survives_restart_and_fix_cycle_and_resets_on_retry(office):
    _, me = office.login()
    store = office.store
    task_id = office.queued_task(me["id"])
    store.promote_queue()
    assert store.attempt_started_at(task_id) is None
    office.set_task(task_id, spec=json.dumps(SPEC))
    assert store.publish_spec(task_id, None, None) == store.get_task(task_id).version
    assert store.published_version(task_id) == store.get_task(task_id).version
    move(store, task_id, TaskStatus.developing)
    started = store.attempt_started_at(task_id)
    assert started is not None
    move(store, task_id, TaskStatus.reviewing, TaskStatus.developing)
    again = TaskStore(Database(office.settings.database_url))
    assert again.attempt_started_at(task_id) == started
    move(store, task_id, TaskStatus.needs_attention)
    retry_decision(store, task_id, me["id"])
    move(store, task_id, TaskStatus.queued, TaskStatus.awaiting_spec_approval)
    assert store.attempt(task_id) == 1
    assert store.attempt_started_at(task_id) is None
    assert store.published_version(task_id) is None
    move(store, task_id, TaskStatus.developing)
    assert store.attempt_started_at(task_id) >= started
    again.db.close()


def test_ensure_status_is_idempotent(office):
    _, me = office.login()
    store = office.store
    task_id = office.queued_task(me["id"])
    store.promote_queue()
    office.set_task(task_id, status="reviewing")
    assert store.ensure_status(task_id, TaskStatus.reviewing, TaskStatus.developing, fix_cycles=2) is False
    assert store.ensure_status(task_id, TaskStatus.reviewing, TaskStatus.developing, fix_cycles=1)
    assert store.ensure_status(task_id, TaskStatus.reviewing, TaskStatus.developing, fix_cycles=1)
    assert store.get_task(task_id).fix_cycles == 1
    assert store.ensure_status(task_id, TaskStatus.developing, TaskStatus.reviewing)
    assert store.ensure_status(task_id, TaskStatus.developing, TaskStatus.reviewing)
    office.set_task(task_id, status="cancelled")
    assert store.ensure_status(task_id, TaskStatus.reviewing, TaskStatus.awaiting_integration) is False
    statuses = [event.payload["to"] for event in store.events_after(task_id, 0, 100) if event.type == "status_changed"]
    assert statuses == ["awaiting_spec_approval", "developing", "reviewing"]


def test_default_app_creates_runner_with_checkpoints(office, origin, tmp_path):
    app = office.new_app(workspace=True, runner=True, target_repo_url=origin.url, workspace_dir=tmp_path / "workspace")
    runner = app.state.runner
    assert runner is not None and app.state.workspace.wait_stopped == runner.wait_stopped
    if office.settings.database_url.startswith("sqlite:///"):
        assert checkpoint_path(office.settings.database_url) == tmp_path / "office.checkpoints.sqlite"
        assert (tmp_path / "office.checkpoints.sqlite").is_file()
    with TestClient(app, base_url="https://testserver"):
        pass
    assert runner.stopping
    assert office.new_app(workspace=True, runner=False, target_repo_url=origin.url,
                          workspace_dir=tmp_path / "workspace").state.runner is None


def test_settings_read_graph_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11500")
    monkeypatch.setenv("OFFICE_MODEL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("OFFICE_TASK_TIME_LIMIT_MINUTES", "15")
    settings = Settings.from_env()
    assert (settings.ollama_url, settings.model_timeout_seconds, settings.task_time_limit_minutes) == (
        "http://127.0.0.1:11500", 45.0, 15.0
    )
    monkeypatch.delenv("OFFICE_MODEL_TIMEOUT_SECONDS")
    monkeypatch.delenv("OFFICE_TASK_TIME_LIMIT_MINUTES")
    assert (Settings.from_env().model_timeout_seconds, Settings.from_env().task_time_limit_minutes) == (None, 60.0)


def test_worktree_removal_waits_for_stopped_run(office, origin, tmp_path):
    app = office.graph_app(origin, tmp_path)
    client, me = office.login(app=app)
    store = app.state.store
    app.state.workspace.wait_stopped = lambda task_id: False
    task_id = store.create_task(me["id"], UI_TASK, ModelProfile.hybrid, IntakeOutcome(True, None)).id
    store.promote_queue()
    wait_for(lambda: store.get_task(task_id).spec, 30, "spec")
    path = app.state.workspace.worktree_path(task_id)
    task = store.get_task(task_id)
    response = client.post(
        f"/api/tasks/{task_id}/decisions",
        json={"decision": "cancel", "expected_version": task.version, "idempotency_key": "cancel-postponed"},
    )
    assert response.status_code == 200
    app.state.workspace.wait_idle()
    assert path.is_dir()
    [event] = [item for item in store.events_after(task_id, 0, 1000) if item.payload.get("result") == "error"]
    assert event.tool == "git_worktree" and "отложено" in event.payload["reason"]
    office.stop_app(app)
    second = office.graph_app(origin, tmp_path)
    second.state.workspace.wait_idle()
    assert not path.exists()
    assert second.state.store.get_task(task_id).status == TaskStatus.cancelled


def test_time_window_uses_event_time(office):
    _, me = office.login()
    store = office.store
    task_id = office.queued_task(me["id"])
    store.promote_queue()
    move(store, task_id, TaskStatus.developing)
    started = store.attempt_started_at(task_id)
    event = [item for item in store.events_after(task_id, 0, 100) if item.payload.get("to") == "developing"][0]
    assert started == event.timestamp
    assert timedelta(0) <= event.timestamp - store.get_task(task_id).created_at < timedelta(minutes=1)
