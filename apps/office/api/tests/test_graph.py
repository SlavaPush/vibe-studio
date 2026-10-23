import json
import threading
import uuid
from datetime import timedelta

import pytest
from conftest import QA_TEST, UI_TASK, ScriptedGateway, developer_answer, git, qa_answer, wait_for
from langgraph.types import Command

from app.db import utcnow
from app.graph import TaskGraph, spec_hash
from app.models_gateway import ProviderReply
from app.roles import Finding
from app.sandbox import E2EResult, SandboxCancelled
from app.schemas import AgentRole, ModelProfile, TaskStatus
from app.tasks import IntakeOutcome


class Crash(BaseException):
    pass


def crash(app):
    with app.state.runner.lock:
        app.state.runner.stopping = True
    raise Crash()


def create(app, owner_id, *, profile=ModelProfile.hybrid, **fields):
    store = app.state.store
    task_id = store.create_task(owner_id, UI_TASK, profile, IntakeOutcome(True, None)).id
    if fields:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with store.db.transaction() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", fields | {"id": task_id})
    store.promote_queue()
    return task_id


def events(app, task_id, kind=None):
    found = app.state.store.events_after(task_id, 0, 10000)
    return [event for event in found if kind is None or event.type == kind]


def status(app, task_id):
    return app.state.store.get_task(task_id).status


def wait_status(app, task_id, *expected, timeout=30):
    return wait_for(lambda: status(app, task_id) in expected, timeout, f"{task_id} in {expected}, now {status(app, task_id)}")


def spec_required(app, task_id, count=1):
    def ready():
        found = [event for event in events(app, task_id, "decision_required") if event.payload["decision"] == "approve_spec"]
        return found if len(found) >= count else None

    return wait_for(ready, 30, "decision_required")[-1].payload["expected_version"]


def decide(client, app, task_id, decision, version=None):
    version = version or app.state.store.get_task(task_id).version
    response = client.post(
        f"/api/tasks/{task_id}/decisions",
        json={"decision": decision, "expected_version": version, "idempotency_key": f"{decision}-{uuid.uuid4().hex[:12]}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, app, task_id, count=1):
    expected = spec_required(app, task_id, count)
    wait_for(lambda: app.state.store.get_task(task_id).version == expected, 10, "version")
    return decide(client, app, task_id, "approve_spec", expected)


def transitions(app, task_id):
    return [(event.payload["from"], event.payload["to"]) for event in events(app, task_id, "status_changed")]


def worktree(app, task_id):
    return app.state.workspace.worktree_path(task_id)


def office_commits(app, task_id, role):
    task = app.state.store.get_task(task_id)
    log = git(worktree(app, task_id), "log", "--format=%H %(trailers:key=Office-Role,valueonly,separator=)", f"{task.base_sha}..HEAD")
    return [line.split()[0] for line in log.splitlines() if line.split()[1:] == [role]]


@pytest.fixture
def graph(office, origin, tmp_path):
    app = office.graph_app(origin, tmp_path)
    client, me = office.login(app=app)
    return app, client, me


def test_full_pass_reaches_awaiting_integration(office, origin, graph):
    app, client, me = graph
    store = app.state.store
    task_id = create(app, me["id"])
    expected = spec_required(app, task_id)
    task = store.get_task(task_id)
    assert task.status == TaskStatus.awaiting_spec_approval
    body = {key: task.spec.model_dump()[key] for key in ("summary", "acceptance_criteria", "sources")}
    assert task.spec.spec_hash == spec_hash(body) and task.spec.spec_hash.startswith("sha256:")
    assert task.spec.acceptance_criteria == ["Кнопка копирования видна под ответом.", "Нажатие копирует текст ответа."]
    assert expected == task.version
    assert (app.state.settings.artifacts_dir / task_id / "spec-1.json").is_file()
    assert app.state.gateway.role_calls("developer") == []

    clone = app.state.workspace.clone
    moved = origin.push("web/src/Other.tsx", "export {}\n")
    git(clone, "fetch", "-q", "origin")
    git(clone, "update-ref", "refs/heads/main", moved)
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")

    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    assert transitions(app, task_id) == [
        ("queued", "awaiting_spec_approval"),
        ("awaiting_spec_approval", "developing"),
        ("developing", "reviewing"),
        ("reviewing", "awaiting_integration"),
    ]
    root = worktree(app, task_id)
    [dev_sha] = office_commits(app, task_id, "developer")
    [qa_sha] = office_commits(app, task_id, "qa")
    assert git(root, "rev-parse", "HEAD") == qa_sha
    assert git(root, "show", f"{dev_sha}:web/src/App.tsx").startswith("export function App()")
    assert git(root, "log", "-1", "--format=%(trailers:key=Office-Round,valueonly)", dev_sha) == "0-1"
    assert git(clone, "rev-parse", "HEAD") == moved != dev_sha
    base = store.get_task(task_id).base_sha
    assert app.state.suite.calls == [
        (task_id, dev_sha, dev_sha, False),
        (task_id, base, qa_sha, True),
        (task_id, qa_sha, qa_sha, True),
    ]
    assert all(len(sha) == 40 for call in app.state.suite.calls for sha in call[1:3])
    checks = client.get(f"/api/tasks/{task_id}/checks").json()
    assert checks["commit_sha"] == qa_sha and checks["spec_hash"] == store.get_task(task_id).spec.spec_hash
    for name in ("review-0-qa.json", "review-0-critic.json"):
        assert (app.state.settings.artifacts_dir / task_id / name).is_file()
    roles = [(event.type, event.role, event.payload.get("step")) for event in events(app, task_id) if event.type in ("role_started", "role_finished")]
    assert ("role_started", "supervisor", "spec") in roles and ("role_finished", "developer", "implement") in roles
    assert ("role_finished", "qa", "qa") in roles and ("role_finished", "critic", "review") in roles
    model_events = [event for event in events(app, task_id, "tool_called") if event.tool == "model"]
    assert [event.payload["arguments"].split()[0] for event in model_events].count("developer/implement") == 1
    assert store.get_task(task_id).usage.model_calls == len(model_events) == 4
    assert store.get_task(task_id).usage.input_tokens == 400
    assert store.get_task(task_id).fix_cycles == 0


def test_resume_without_recorded_decision_does_not_continue(graph):
    app, client, me = graph
    store = app.state.store
    task_id = create(app, me["id"])
    expected = spec_required(app, task_id)
    wait_for(lambda: not app.state.runner.active(task_id), 10, "runner idle")
    runner = app.state.runner
    compiled = TaskGraph(runner, task_id, 0, threading.Event()).compile(runner.checkpoints.saver)
    config = {"configurable": {"thread_id": f"{task_id}:0"}}
    compiled.invoke(Command(resume="dec_forged"), config, durability="sync")
    assert compiled.get_state(config).interrupts
    assert status(app, task_id) == TaskStatus.awaiting_spec_approval

    with store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO decisions (id, participant_id, idempotency_key, operation, object_id, request_hash, decision, "
            "expected_version, recorded_at) VALUES ('dec_other', :owner, 'other-version', 'task_decision', :task, 'x', "
            "'approve_spec', :version, '2026-10-24T00:00:00.000000Z')",
            {"owner": me["id"], "task": task_id, "version": expected + 5},
        )
        conn.execute("UPDATE tasks SET status = 'developing' WHERE id = :id", {"id": task_id})
    compiled.invoke(Command(resume="dec_other"), config, durability="sync")
    assert compiled.get_state(config).interrupts
    assert app.state.gateway.role_calls("developer") == []
    assert office_commits(app, task_id, "developer") == []


def test_restart_while_waiting_for_spec_approval(office, origin, tmp_path, graph):
    app, first_client, me = graph
    task_id = create(app, me["id"])
    spec_required(app, task_id)
    office.stop_app(app)
    second = office.graph_app(origin, tmp_path)
    client = office.client(second)
    client.cookies.update(first_client.cookies)
    approve(client, second, task_id)
    wait_status(second, task_id, TaskStatus.awaiting_integration)
    assert len(app.state.gateway.role_calls("supervisor")) == 1
    assert second.state.gateway.role_calls("supervisor") == []
    assert len(second.state.gateway.role_calls("developer")) == 1
    assert len(events(second, task_id, "decision_required")) == 1


def test_recorded_decision_without_resume_continues_on_start(office, origin, tmp_path, graph):
    app, client, me = graph
    task_id = create(app, me["id"])
    expected = spec_required(app, task_id)
    office.stop_app(app)
    decide(client, app, task_id, "approve_spec", expected)
    assert status(app, task_id) == TaskStatus.developing
    second = office.graph_app(origin, tmp_path)
    wait_status(second, task_id, TaskStatus.awaiting_integration)
    assert len(second.state.gateway.role_calls("developer")) == 1


def test_restart_during_develop_continues_from_checkpoint(office, origin, tmp_path, graph):
    app, client, me = graph
    gateway = app.state.gateway
    entered = threading.Event()
    release = threading.Event()

    def blocked(call):
        entered.set()
        release.wait(30)
        return developer_answer("export const stale = 1\n")

    gateway.handlers["developer"] = blocked
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    assert entered.wait(30)
    office.stop_app(app, timeout=0.5)

    second = office.graph_app(origin, tmp_path)
    wait_status(second, task_id, TaskStatus.awaiting_integration)
    release.set()
    wait_for(lambda: not app.state.runner.active(task_id), 10, "old run stopped")
    root = worktree(second, task_id)
    assert "stale" not in (root / "web/src/App.tsx").read_text()
    assert len(office_commits(second, task_id, "developer")) == 1
    writes = [event for event in events(second, task_id, "tool_called") if event.tool == "write_file" and event.role == "developer"]
    assert len(writes) == 1
    assert second.state.store.get_task(task_id).usage.model_calls == 5


def test_crash_after_developer_commit_does_not_commit_again(office, origin, tmp_path, graph):
    app, client, me = graph
    workspace = app.state.workspace
    original = workspace.commit

    def failing_commit(task_id, message, role=None):
        original(task_id, message, role)
        crash(app)

    workspace.commit = failing_commit
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_for(lambda: office_commits(app, task_id, "developer"), 30, "developer commit")
    wait_for(lambda: not app.state.runner.active(task_id), 10, "crashed run")
    office.stop_app(app)
    assert status(app, task_id) == TaskStatus.developing

    second = office.graph_app(origin, tmp_path)
    wait_status(second, task_id, TaskStatus.awaiting_integration)
    assert len(app.state.gateway.role_calls("developer")) == 1
    assert second.state.gateway.role_calls("developer") == []
    assert len(office_commits(second, task_id, "developer")) == 1


@pytest.mark.parametrize("target", ["reviewing", "developing"])
def test_crash_after_transition_does_not_repeat_it(office, origin, tmp_path, graph, target):
    app, client, me = graph
    store = app.state.store
    original = store.ensure_status
    if target == "developing":
        reviews = iter([{"verdict": "changes_requested", "findings": [
            {"blocking": True, "criterion": 1, "file": "web/src/App.tsx", "line": 2, "text": "Кнопки нет."}
        ]}])
        app.state.gateway.handlers["critic"] = lambda call: next(reviews, {"verdict": "pass", "findings": []})

    def failing_status(task_id, source, goal, *, fix_cycles=None):
        result = original(task_id, source, goal, fix_cycles=fix_cycles)
        if goal == TaskStatus(target) and source != TaskStatus.awaiting_spec_approval:
            crash(app)
        return result

    store.ensure_status = failing_status
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus(target))
    wait_for(lambda: not app.state.runner.active(task_id), 30, "crashed run")
    office.stop_app(app)

    fresh = ScriptedGateway(app.state.settings)
    fresh.versions = 100
    second = office.graph_app(origin, tmp_path, gateway=fresh)
    wait_status(second, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(second, task_id) == TaskStatus.awaiting_integration
    task = second.state.store.get_task(task_id)
    if target == "reviewing":
        assert transitions(second, task_id).count(("developing", "reviewing")) == 1
        assert task.fix_cycles == 0
    else:
        assert transitions(second, task_id).count(("reviewing", "developing")) == 1
        assert task.fix_cycles == 1
        assert len(office_commits(second, task_id, "developer")) == 2


def test_crash_after_qa_commit_keeps_qa_findings(office, origin, tmp_path, graph):
    app, client, me = graph
    finding = {"blocking": True, "criterion": 1, "file": None, "line": None, "text": "Кнопка не подписана."}
    app.state.gateway.handlers["qa"] = lambda call: qa_answer(call, findings=[finding])
    workspace = app.state.workspace
    original = workspace.commit

    def failing_commit(task_id, message, role=None):
        sha = original(task_id, message, role)
        if role == AgentRole.qa:
            crash(app)
        return sha

    workspace.commit = failing_commit
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_for(lambda: office_commits(app, task_id, "qa"), 30, "qa commit")
    wait_for(lambda: not app.state.runner.active(task_id), 30, "crashed run")
    office.stop_app(app)
    assert status(app, task_id) == TaskStatus.reviewing

    fresh = ScriptedGateway(app.state.settings)
    fresh.versions = 100
    second = office.graph_app(origin, tmp_path, gateway=fresh)
    wait_status(second, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(second, task_id) == TaskStatus.awaiting_integration
    assert second.state.store.get_task(task_id).fix_cycles == 1
    saved = json.loads((second.state.settings.artifacts_dir / task_id / "review-0-qa.json").read_text())
    assert saved["findings"] == [{"id": "qa-0-1", "source": "qa"} | finding]
    assert len(fresh.role_calls("qa")) == 1
    assert "Кнопка не подписана." in fresh.role_calls("developer")[0].user


def test_immediate_approval_after_retry_continues_new_attempt(graph):
    app, client, me = graph
    gateway = app.state.gateway
    entered = threading.Event()
    release = threading.Event()

    def critic(call):
        if not entered.is_set():
            entered.set()
            release.wait(30)
        return {"verdict": "pass", "findings": []}

    gateway.handlers["critic"] = critic
    app.state.suite.e2e = lambda task_id, app_sha, test_file: E2EResult("passed")
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    assert entered.wait(30)
    wait_status(app, task_id, TaskStatus.needs_attention)
    decide(client, app, task_id, "retry")
    wait_status(app, task_id, TaskStatus.awaiting_spec_approval)
    decide(client, app, task_id, "approve_spec")
    assert status(app, task_id) == TaskStatus.developing
    app.state.suite.e2e = lambda task_id, app_sha, test_file: (
        E2EResult("failed", line=1, message="x") if app_sha == app.state.store.get_task(task_id).base_sha else E2EResult("passed")
    )
    release.set()
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    assert status(app, task_id) == TaskStatus.awaiting_integration
    assert len(events(app, task_id, "decision_required")) == 1
    assert len(office_commits(app, task_id, "developer")) == 2


def test_halt_in_one_branch_stops_containers_of_other_branch(graph):
    app, client, me = graph
    store = app.state.store
    suite = app.state.suite
    qa_running = threading.Event()
    interrupted = []

    def on_suite(task_id, app_sha, qa_sha, e2e):
        if e2e:
            qa_running.set()
            wait_for(lambda: task_id in suite.cancelled, 10, "cancel of QA container")
            interrupted.append(app_sha)
            raise SandboxCancelled("cancelled")

    suite.on_suite = on_suite
    clock = store.attempt_started_at
    expired = threading.Event()
    store.attempt_started_at = lambda task_id: utcnow() - timedelta(hours=2) if expired.is_set() else clock(task_id)

    def critic(call):
        assert qa_running.wait(10)
        expired.set()
        return {"verdict": "pass", "findings": []}

    app.state.gateway.handlers["critic"] = critic
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    wait_for(lambda: not app.state.runner.active(task_id), 10, "run stopped")
    assert len(interrupted) == 1
    assert len([call for call in suite.calls if call[3]]) == 1
    halts = [event for event in events(app, task_id, "role_finished") if event.payload["outcome"] == "blocked"]
    assert halts[-1].payload["summary"] == "Превышен предел времени выполнения задачи."
    assert not (app.state.settings.artifacts_dir / task_id / "review-0-qa.json").exists()


def test_recovery_removes_stale_containers_first(graph):
    app, _, _ = graph
    assert app.state.suite.stale_removed == 1


def test_decision_recorded_before_interrupt_is_saved(graph):
    app, client, me = graph
    store = app.state.store
    original = store.spec_approval
    state = {"first": True}

    def late(task_id, expected):
        if state["first"]:
            state["first"] = False
            decide(client, app, task_id, "approve_spec", expected)
            return None
        return original(task_id, expected)

    store.spec_approval = late
    task_id = create(app, me["id"])
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    assert len(app.state.gateway.role_calls("developer")) == 1


def test_crash_after_publish_does_not_publish_again(office, origin, tmp_path, graph):
    app, client, me = graph
    store = app.state.store
    original = store.publish_spec

    def failing_publish(*args):
        original(*args)
        crash(app)

    store.publish_spec = failing_publish
    task_id = create(app, me["id"])
    expected = spec_required(app, task_id)
    wait_for(lambda: not app.state.runner.active(task_id), 10, "crashed run")
    office.stop_app(app)
    decide(client, app, task_id, "approve_spec", expected)

    second = office.graph_app(origin, tmp_path)
    wait_status(second, task_id, TaskStatus.awaiting_integration)
    assert len(events(second, task_id, "decision_required")) == 1
    assert len(events(second, task_id, "artifact_ready")) == 3
    assert len(app.state.gateway.role_calls("supervisor")) == 1


def test_failed_checks_return_to_developer(graph):
    app, client, me = graph
    suite = app.state.suite
    first = {}

    def exits(task_id, app_sha, qa_sha, e2e):
        first.setdefault("sha", app_sha)
        return {"test": 1} if app_sha == first["sha"] else {}

    suite.exits = exits
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    calls = app.state.gateway.role_calls("developer")
    assert len(calls) == 2
    assert "npm run test: код выхода 1" in calls[1].user
    commits = office_commits(app, task_id, "developer")
    assert len(commits) == 2 and commits[0] != commits[1]
    assert [call[1] for call in suite.calls[:2]] == list(reversed(commits))
    assert app.state.store.get_task(task_id).fix_cycles == 0


def test_checks_failing_three_times_need_attention(graph):
    app, client, me = graph
    app.state.suite.exits = lambda task_id, app_sha, qa_sha, e2e: {"build": 2}
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    assert len(app.state.gateway.role_calls("developer")) == 3
    assert events(app, task_id, "role_finished")[-1].payload["step"] == "verify"


def test_three_graphs_run_and_fourth_waits(graph):
    app, client, me = graph
    gateway = app.state.gateway
    release = {}
    lock = threading.Lock()

    def blocked(call):
        with lock:
            event = release.setdefault(call.task_id, threading.Event())
        event.wait(30)
        return {
            "summary": "x", "acceptance_criteria": ["y"], "needs_research": False, "research_questions": [],
        }

    gateway.handlers["supervisor"] = blocked
    ids = [create(app, me["id"]) for _ in range(4)]
    wait_for(lambda: len(release) == 3, 30, "three graphs")
    assert set(release) == set(ids[:3])
    assert status(app, ids[3]) == TaskStatus.queued
    assert all(app.state.runner.active(task_id) for task_id in ids[:3])
    paths = {str(worktree(app, task_id)) for task_id in ids[:3]}
    assert len(paths) == 3 and all(worktree(app, task_id).is_dir() for task_id in ids[:3])

    decide(client, app, ids[0], "cancel")
    release[ids[0]].set()
    wait_for(lambda: ids[3] in release, 30, "fourth graph")
    assert status(app, ids[3]) == TaskStatus.awaiting_spec_approval
    for event in list(release.values()):
        event.set()
    for task_id in ids[1:]:
        spec_required(app, task_id)
    assert app.state.store.get_task(ids[0]).spec is None


def test_blocking_critic_finding_returns_to_developer_until_limit(graph):
    app, client, me = graph
    finding = {"blocking": True, "criterion": None, "file": "web/src/App.tsx", "line": 2, "text": "Кнопка не подписана."}
    app.state.gateway.handlers["critic"] = lambda call: {"verdict": "changes_requested", "findings": [finding]}
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    task = app.state.store.get_task(task_id)
    assert task.fix_cycles == 2
    assert transitions(app, task_id).count(("reviewing", "developing")) == 2
    developer = app.state.gateway.role_calls("developer")
    assert len(developer) == 3
    assert "Кнопка не подписана." in developer[1].user and "web/src/App.tsx" in developer[1].user
    artifact = json.loads((app.state.settings.artifacts_dir / task_id / "review-0-critic.json").read_text())
    [saved] = artifact["findings"]
    assert Finding.model_validate(saved).model_dump() == saved
    assert saved | {} == {"id": "critic-0-1", "source": "critic"} | finding
    assert len(office_commits(app, task_id, "developer")) == 3
    assert (app.state.settings.artifacts_dir / task_id / "review-2-critic.json").is_file()
    assert events(app, task_id, "role_finished")[-1].payload["outcome"] == "blocked"


def test_qa_failure_on_task_version_becomes_blocking_finding(graph):
    app, client, me = graph
    suite = app.state.suite
    rounds = {"after": 0}

    def e2e(task_id, app_sha, test_file):
        if app_sha == app.state.store.get_task(task_id).base_sha:
            return E2EResult("failed", line=4, message="locator not found")
        rounds["after"] += 1
        if rounds["after"] == 1:
            return E2EResult("failed", line=7, message="Expected visible button")
        return E2EResult("passed")

    suite.e2e = e2e
    app.state.gateway.handlers["qa"] = lambda call: qa_answer(call, criteria=[2])
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    data = json.loads((app.state.settings.artifacts_dir / task_id / "review-0-qa.json").read_text())
    [finding] = data["findings"]
    assert Finding.model_validate(finding)
    assert finding == {
        "id": "qa-0-e2e",
        "source": "qa",
        "blocking": True,
        "criterion": 2,
        "file": f"web/e2e/office/{task_id}.pw.ts",
        "line": 7,
        "text": "Expected visible button",
    }
    developer = app.state.gateway.role_calls("developer")
    assert len(developer) == 2 and "Expected visible button" in developer[1].user
    assert app.state.store.get_task(task_id).fix_cycles == 1
    assert json.loads((app.state.settings.artifacts_dir / task_id / "review-1-qa.json").read_text())["findings"] == []


def test_qa_test_passing_on_base_is_rejected(graph):
    app, client, me = graph
    app.state.suite.e2e = lambda task_id, app_sha, test_file: E2EResult("passed")
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    qa_calls = app.state.gateway.role_calls("qa")
    assert len(qa_calls) == 2
    assert "Тест прошёл на исходной версии" in qa_calls[1].user
    finished = [event for event in events(app, task_id, "role_finished") if event.role == "qa"]
    assert finished[-1].payload["summary"] == "Тест QA проходит на исходной версии после двух попыток."
    assert not (app.state.settings.artifacts_dir / task_id / "review-0-qa.json").exists()


def test_qa_rewrite_after_base_pass_is_accepted(graph):
    app, client, me = graph
    store = app.state.store
    seen = {"base": 0}

    def e2e(task_id, app_sha, test_file):
        if app_sha == store.get_task(task_id).base_sha:
            seen["base"] += 1
            return E2EResult("passed") if seen["base"] == 1 else E2EResult("failed", line=3, message="no button")
        return E2EResult("passed")

    app.state.suite.e2e = e2e
    answers = iter([QA_TEST, QA_TEST.replace("feature", "feature v2")])

    def qa(call):
        answer = qa_answer(call)
        answer["actions"][0]["content"] = next(answers)
        return answer

    app.state.gateway.handlers["qa"] = qa
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    assert len(office_commits(app, task_id, "qa")) == 2


def test_missing_qa_evidence_is_retried_once(graph):
    app, client, me = graph
    app.state.suite.e2e = lambda task_id, app_sha, test_file: E2EResult("no_evidence", "Сборка приложения для e2e не прошла.")
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    assert len([call for call in app.state.suite.calls if call[3]]) == 2
    finished = [event for event in events(app, task_id, "role_finished") if event.role == "qa"]
    assert finished[-1].payload["summary"].startswith("Нет свидетельства QA")


def test_budget_allows_thirtieth_call_and_stops_thirty_first(graph):
    app, client, me = graph
    task_id = create(app, me["id"], model_calls=28)
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    task = app.state.store.get_task(task_id)
    assert task.usage.model_calls == 30
    assert len(office_commits(app, task_id, "developer")) == 1
    last = [event for event in events(app, task_id, "role_finished")][-1]
    assert last.payload["summary"] == "Исчерпан предел обращений к моделям для задачи."


def test_model_call_is_counted_before_provider(graph):
    app, client, me = graph
    seen = []
    store = app.state.store

    def supervisor(call):
        seen.append(store.get_task(call.task_id).usage.model_calls)
        return {"summary": "x", "acceptance_criteria": ["y"], "needs_research": False, "research_questions": []}

    app.state.gateway.handlers["supervisor"] = supervisor
    task_id = create(app, me["id"], model_calls=2)
    spec_required(app, task_id)
    assert seen == [3]


def test_time_limit_needs_attention(office, origin, tmp_path):
    app = office.graph_app(origin, tmp_path, task_time_limit_minutes=0.002)
    client, me = office.login(app=app)
    original = app.state.gateway.handlers["developer"]

    def slow(call):
        threading.Event().wait(0.3)
        return original(call)

    app.state.gateway.handlers["developer"] = slow
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.needs_attention)
    assert office_commits(app, task_id, "developer") == []
    assert events(app, task_id, "role_finished")[-1].payload["summary"] == "Превышен предел времени выполнения задачи."


def test_cancel_during_model_answer_discards_it(graph):
    app, client, me = graph
    entered = threading.Event()
    release = threading.Event()

    def blocked(call):
        entered.set()
        release.wait(30)
        return developer_answer("export const late = 1\n")

    app.state.gateway.handlers["developer"] = blocked
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    assert entered.wait(30)
    root = worktree(app, task_id)
    before = (root / "web/src/App.tsx").read_text()
    decide(client, app, task_id, "cancel")
    threading.Event().wait(0.3)
    assert root.is_dir()
    release.set()
    wait_for(lambda: not root.exists(), 30, "worktree removed")
    assert not app.state.runner.active(task_id)
    assert status(app, task_id) == TaskStatus.cancelled
    assert transitions(app, task_id)[-1] == ("developing", "cancelled")
    assert not [event for event in events(app, task_id, "tool_called") if event.tool == "write_file"]
    assert "late" not in before
    assert task_id in app.state.suite.cancelled


def test_deferred_launch_does_not_restart_cancelled_task(graph):
    app, client, me = graph
    runner = app.state.runner
    entered = threading.Event()
    release = threading.Event()

    def blocked(call):
        entered.set()
        release.wait(30)
        return developer_answer("export const late = 1\n")

    app.state.gateway.handlers["developer"] = blocked
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    assert entered.wait(30)
    run = runner.runs[task_id]
    launch = runner.launch
    relaunched = threading.Event()

    def deferred(name):
        launch(name)
        relaunched.set()

    runner.launch = deferred
    launch(task_id)
    assert run.again and run.active
    decide(client, app, task_id, "cancel")
    wait_for(run.cancel.is_set, 10, "run cancelled")
    assert task_id in app.state.suite.cancelled
    release.set()
    assert relaunched.wait(30)
    assert runner.runs[task_id] is run and not run.active
    assert task_id in app.state.suite.cancelled
    assert status(app, task_id) == TaskStatus.cancelled


def test_retry_waits_for_running_branch_and_keeps_usage(graph):
    app, client, me = graph
    gateway = app.state.gateway
    entered = threading.Event()
    release = threading.Event()
    reviews = {"count": 0}

    def critic(call):
        reviews["count"] += 1
        if reviews["count"] == 1:
            entered.set()
            release.wait(30)
        return {"verdict": "pass", "findings": []}

    gateway.handlers["critic"] = critic
    app.state.suite.e2e = lambda task_id, app_sha, test_file: E2EResult("passed")
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    assert entered.wait(30)
    wait_status(app, task_id, TaskStatus.needs_attention)
    before = app.state.store.get_task(task_id)
    decide(client, app, task_id, "retry")
    wait_status(app, task_id, TaskStatus.awaiting_spec_approval)
    threading.Event().wait(0.3)
    assert len(events(app, task_id, "decision_required")) == 1
    assert app.state.runner.active(task_id)
    release.set()
    expected = spec_required(app, task_id, 2)
    critic_artifacts = list((app.state.settings.artifacts_dir / task_id).glob("review-*-critic.json"))
    assert critic_artifacts == []
    task = app.state.store.get_task(task_id)
    assert task.spec == before.spec
    assert task.usage.model_calls >= before.usage.model_calls
    assert task.fix_cycles == before.fix_cycles
    assert len(gateway.role_calls("supervisor")) == 1
    gateway.handlers["critic"] = lambda call: {"verdict": "pass", "findings": []}
    app.state.suite.e2e = lambda task_id, app_sha, test_file: (
        E2EResult("failed", line=1, message="x") if app_sha == app.state.store.get_task(task_id).base_sha else E2EResult("passed")
    )
    decide(client, app, task_id, "approve_spec", expected)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    snapshot = TaskGraph(app.state.runner, task_id, 1, threading.Event()).compile(app.state.runner.checkpoints.saver)
    assert snapshot.get_state({"configurable": {"thread_id": f"{task_id}:1"}}).values["attempt"] == 1
    developer_commits = office_commits(app, task_id, "developer")
    assert len(developer_commits) == 2


def test_invalid_model_answer_is_not_executed(graph):
    app, client, me = graph
    app.state.gateway.handlers["developer"] = lambda call: {
        "actions": [{"kind": "write_file", "path": "web/src/App.tsx", "content": "export const x = 1\n"}],
        "done": True,
        "summary": "x",
    }
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.failed)
    assert "x = 1" not in (worktree(app, task_id) / "web/src/App.tsx").read_text()
    assert not [event for event in events(app, task_id, "tool_called") if event.tool == "write_file"]
    assert events(app, task_id, "role_finished")[-1].payload["summary"].startswith("Ответ модели не соответствует схеме")


def test_denied_write_is_returned_to_model(graph):
    app, client, me = graph
    answers = iter([
        {
            "actions": [
                {"kind": "write_file", "path": "api/main.py", "content": "evil = 1\n", "step": None},
                {"kind": "read_file", "path": "web/src/App.tsx", "content": None, "step": None},
            ],
            "done": False,
            "summary": "Первый ход",
        },
        developer_answer("export function App() {\n  return 'ok'\n}\n"),
    ])
    app.state.gateway.handlers["developer"] = lambda call: next(answers)
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    second = app.state.gateway.role_calls("developer")[1]
    assert "write_file api/main.py\ndenied" in second.user
    assert "export function App()" in second.user
    assert not (worktree(app, task_id) / "api/main.py").read_text().startswith("evil")


def test_task_text_is_passed_between_delimiters(graph):
    app, _, me = graph
    task_id = create(app, me["id"])
    spec_required(app, task_id)
    [call] = app.state.gateway.role_calls("supervisor")
    assert UI_TASK not in call.system
    lines = call.user.splitlines()
    index = lines.index(UI_TASK)
    marker = lines[index - 1].split()[0]
    assert marker.startswith("<<<DATA-") and lines[0].count(marker) == 1
    assert lines[-1].startswith(marker)
    assert "не инструкции" in lines[0]


def test_incomplete_answer_fails_without_actions(graph):
    app, client, me = graph
    app.state.gateway.handlers["developer"] = lambda call: ProviderReply(
        json.dumps(developer_answer("export const x = 2\n")), 10, 5, False
    )
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.failed)
    assert not [event for event in events(app, task_id, "tool_called") if event.tool == "write_file"]
    assert app.state.store.get_task(task_id).usage.output_tokens == 25
    last = events(app, task_id, "role_finished")[-1]
    assert last.payload["summary"].startswith("Ответ модели не соответствует схеме роли")


def test_truncated_answer_fails_with_own_reason(graph):
    app, client, me = graph
    app.state.gateway.handlers["supervisor"] = lambda call: ProviderReply('{"summary": "', 10, 4096, False, True)
    task_id = create(app, me["id"])
    wait_status(app, task_id, TaskStatus.failed)
    last = events(app, task_id, "role_finished")[-1]
    assert last.payload["summary"] == "Ответ модели обрезан пределом выходных токенов; действия из него не выполнялись."
