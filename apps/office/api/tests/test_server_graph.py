import json
import threading

import pytest
from conftest import QA_PYTEST, git, spec_answer
from test_graph import (  # noqa: F401
    approve, create, events, graph, office_commits, spec_required, status, wait_status, worktree
)

from app.graph import TaskGraph, commit_message
from app.roles import CRITIC, CRITIC_API, DEVELOPER, DEVELOPER_API, QA, QA_API, SUPERVISOR_API
from app.schemas import AgentRole, ChangeClass, CheckResult, Contour, TaskStatus

PACKAGE = '{"name": "web", "dependencies": {"left-pad": "^1.3.0"}}\n'


def server_answer(call, extra=None):
    actions = [{"kind": "write_file", "path": "apps/chat/api/app/notes.py", "content": "NOTES = []\n", "step": None}]
    for path, content in (extra or {}).items():
        actions.append({"kind": "write_file", "path": path, "content": content, "step": None})
    return {"actions": actions, "done": True, "summary": "Заметки на сервере"}


@pytest.mark.pg
def test_backend_task_uses_server_prompts_and_pytest_qa(office, origin, graph):
    app, client, me = graph
    gateway = app.state.gateway
    gateway.handlers["developer"] = server_answer
    task_id = create(app, me["id"], change_class="backend_api")
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    systems = {role: {call.system for call in gateway.role_calls(role, task_id)} for role in ("supervisor", "developer", "qa", "critic")}
    assert systems == {
        "supervisor": {SUPERVISOR_API}, "developer": {DEVELOPER_API}, "qa": {QA_API}, "critic": {CRITIC_API}
    }
    [qa_call] = gateway.role_calls("qa", task_id)
    test_file = f"apps/chat/api/tests/office/test_{task_id}.py"
    assert test_file in qa_call.user and "apps/chat/api/tests/conftest.py" in qa_call.user
    root = worktree(app, task_id)
    [qa_sha] = office_commits(app, task_id, "qa")
    assert git(root, "show", f"{qa_sha}:{test_file}") == QA_PYTEST.rstrip("\n")
    task = app.state.store.get_task(task_id)
    assert task.contour == Contour.server and "apps/chat/api/app/notes.py" in task.changed_files
    assert task.change_class == ChangeClass.backend_api
    assert all("api-test" in steps for steps in app.state.suite.steps)
    assert app.state.suite.lock_node.calls == [] and app.state.suite.python.lock.calls == []


@pytest.mark.pg
def test_backend_task_without_server_change_moves_to_frontend(office, origin, graph):
    app, client, me = graph
    gateway = app.state.gateway
    gateway.handlers["supervisor"] = lambda call: spec_answer(call, server_change=False)
    task_id = create(app, me["id"], change_class="backend_api")
    spec_required(app, task_id)
    assert app.state.store.get_task(task_id).change_class == ChangeClass.frontend_ui
    [spec_event] = [event for event in events(app, task_id, "role_finished") if event.role == "supervisor"]
    assert spec_event.payload["summary"] == (
        "Требования: 2 критериев приёмки. Серверных изменений не нужно: задача переведена в контур интерфейса."
    )
    spec = json.loads((app.state.settings.artifacts_dir / task_id / "spec-1.json").read_text(encoding="utf-8"))
    assert "server_change" not in spec
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    systems = {role: {call.system for call in gateway.role_calls(role, task_id)} for role in ("supervisor", "developer", "qa", "critic")}
    assert systems == {"supervisor": {SUPERVISOR_API}, "developer": {DEVELOPER}, "qa": {QA}, "critic": {CRITIC}}
    [qa_call] = gateway.role_calls("qa", task_id)
    assert f"apps/chat/web/e2e/office/{task_id}.pw.ts" in qa_call.user
    task = app.state.store.get_task(task_id)
    assert task.contour == Contour.ui and task.change_class == ChangeClass.frontend_ui
    assert not any("api-test" in steps for steps in app.state.suite.steps)


@pytest.mark.pg
def test_frontend_developer_cannot_write_server_zone(office, origin, graph):
    app, client, me = graph
    app.state.gateway.handlers["developer"] = lambda call: server_answer(call, {"apps/chat/web/src/App.tsx": "export {}\n"})
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    denied = [
        event for event in events(app, task_id, "tool_called")
        if event.tool == "write_file" and event.payload["result"] == "denied"
    ]
    assert denied and "серверной зоны" in denied[0].payload["reason"]
    task = app.state.store.get_task(task_id)
    assert "apps/chat/api/app/notes.py" not in task.changed_files
    assert task.contour == Contour.ui


@pytest.mark.pg
def test_manifest_change_gets_office_lock_commit_once(office, origin, graph):
    app, client, me = graph
    suite = app.state.suite
    app.state.gateway.handlers["developer"] = lambda call: server_answer(call, {"apps/chat/web/package.json": PACKAGE})
    task_id = create(app, me["id"], change_class="backend_api")
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    root = worktree(app, task_id)
    [dev_sha] = office_commits(app, task_id, "developer")
    [lock_sha] = office_commits(app, task_id, "office")
    assert git(root, "rev-parse", f"{lock_sha}^") == dev_sha
    assert git(root, "diff", "--name-only", dev_sha, lock_sha) == "apps/chat/web/package-lock.json"
    assert git(root, "log", "-1", "--format=%(trailers:key=Office-Round,valueonly)", lock_sha) == "0-1"
    assert len(git(root, "log", "-1", "--format=%(trailers:key=Office-Manifests,valueonly)", lock_sha)) == 64
    assert suite.lock_node.calls == [task_id] and suite.python.lock.calls == []
    assert suite.calls[0][1] == lock_sha
    task = app.state.store.get_task(task_id)
    assert "apps/chat/web/package-lock.json" in task.changed_files

    graph_run = TaskGraph(app.state.runner, task_id, 0, threading.Event())
    head = git(root, "rev-parse", "HEAD")
    with app.state.store.db.transaction() as conn:
        conn.execute("UPDATE tasks SET status = 'developing' WHERE id = :id", {"id": task_id})
    git(root, "reset", "-q", "--hard", lock_sha)
    state = graph_run.develop({"dev_round": 1, "round_base_sha": task.base_sha})
    assert state["dev_sha"] == lock_sha and suite.lock_node.calls == [task_id]
    git(root, "reset", "-q", "--hard", dev_sha)
    state = graph_run.develop({"dev_round": 1, "round_base_sha": task.base_sha})
    assert suite.lock_node.calls == [task_id, task_id]
    assert len(app.state.gateway.role_calls("developer", task_id)) == 1
    assert state["dev_sha"] != dev_sha and git(root, "rev-parse", f"{state['dev_sha']}^") == dev_sha
    assert head != dev_sha


@pytest.mark.pg
def test_lock_files_follow_current_manifest(office, origin, graph):
    app, client, me = graph
    suite = app.state.suite
    workspace = app.state.workspace
    app.state.gateway.handlers["developer"] = lambda call: server_answer(call, {"apps/chat/web/package.json": PACKAGE})
    task_id = create(app, me["id"], change_class="backend_api")
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    root = worktree(app, task_id)
    task = app.state.store.get_task(task_id)
    base_manifest = git(root, "show", f"{task.base_sha}:apps/chat/web/package.json") + "\n"
    base_lock = git(root, "show", f"{task.base_sha}:apps/chat/web/package-lock.json")
    with app.state.store.db.transaction() as conn:
        conn.execute("UPDATE tasks SET status = 'developing' WHERE id = :id", {"id": task_id})
    graph_run = TaskGraph(app.state.runner, task_id, 0, threading.Event())

    def round_with(content, marker):
        (root / "apps/chat/web/package.json").write_text(content, encoding="utf-8")
        assert workspace.commit(task_id, commit_message("Манифест", AgentRole.developer, marker), AgentRole.developer)
        return graph_run.lock_files(task, marker)

    other = '{"name": "web", "dependencies": {"left-pad": "^1.2.0"}}\n'
    assert len(suite.lock_node.calls) == 1
    round_with(other, "0-2")
    assert len(suite.lock_node.calls) == 2
    round_with(PACKAGE, "0-3")
    assert len(suite.lock_node.calls) == 3
    state = round_with(base_manifest, "0-4")
    assert len(suite.lock_node.calls) == 3
    assert git(root, "show", f"{state['dev_sha']}:apps/chat/web/package-lock.json") == base_lock
    assert git(root, "log", "-1", "--format=%(trailers:key=Office-Role,valueonly)", state["dev_sha"]) == "office"
    assert graph_run.lock_files(task, "0-4")["dev_sha"] == state["dev_sha"]
    assert len(office_commits(app, task_id, "office")) == 4
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.pg
def test_failed_lock_step_returns_to_developer(office, origin, graph):
    app, client, me = graph
    suite = app.state.suite
    real = suite.lock_node
    calls = []

    def flaky(task_id, root):
        calls.append(task_id)
        if len(calls) == 1:
            return CheckResult(command="npm install --package-lock-only", exit_code=1, duration_ms=1, artifact_urls=[])
        return real(task_id, root)

    suite.lock_node = flaky
    app.state.gateway.handlers["developer"] = lambda call: server_answer(call, {"apps/chat/web/package.json": PACKAGE})
    task_id = create(app, me["id"], change_class="backend_api")
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    developer = app.state.gateway.role_calls("developer", task_id)
    assert len(developer) == 2
    assert "npm install --package-lock-only: код выхода 1" in developer[1].user
    assert len(calls) == 2
    assert len(office_commits(app, task_id, "office")) == 1
    root = worktree(app, task_id)
    assert git(root, "status", "--porcelain") == ""
    assert json.loads((root / "apps/chat/web/package.json").read_text())["dependencies"] == {"left-pad": "^1.3.0"}
