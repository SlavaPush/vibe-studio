import base64
import json
import logging
import secrets
import subprocess
import threading
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import release_queue
from app import workspace as workspace_module
from app.config import CHECKOUT_ROOT
from app.db import utcnow
from app.github import GitHubError, GitHubUnavailable, github_client
from app.models_gateway import ProviderReply
from app.operations import OperationLog, OpStatus
from app.release import ReleaseWorker, task_carrier
from app.required_checks import load_required_checks
from app.sandbox import SandboxError
from app.schemas import AgentRole, CheckReport, CheckResult, FailureKind, ReleaseStatus, TaskEventType, TaskStatus
from app.service_check import SERVICE_TIMEOUT
from app.workspace import PushResult, Workspace
from conftest import CI_JOBS, MODEL_TITLE, FakeGitHub, FakeSuite, ScriptedGateway, git

pytestmark = pytest.mark.pg

SPEC = {
    "version": 1,
    "spec_hash": "sha256:spec",
    "summary": "Под ответом ассистента появляется кнопка копирования.",
    "acceptance_criteria": ["Кнопка копирования видна под ответом.", "Нажатие копирует текст ответа."],
    "sources": [],
}
REQUIRED = load_required_checks(CHECKOUT_ROOT)
TABLES = (
    "participants", "sessions", "tasks", "task_events", "decisions", "release_candidates", "check_reports",
    "intake_reservations", "operations",
)


class Crash(BaseException):
    pass


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self):
        return utcnow() + self.offset


class Service:
    def __init__(self, version: str) -> None:
        self.version: object = version
        self.status = 200
        self.down = False
        self.timeouts = 0
        self.tick: timedelta | None = None
        self.clock: Clock | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.tick is not None and self.clock is not None:
            self.clock.offset += self.tick
        if self.timeouts:
            self.timeouts -= 1
            raise httpx.ReadTimeout("timed out", request=request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(self.status, json={"version": self.version})


class Setup(SimpleNamespace):
    def worker(self) -> ReleaseWorker:
        if getattr(self, "current", None) is not None:
            self.current.shutdown()
        self.current = ReleaseWorker(
            self.settings, self.store, self.workspace, self.github, REQUIRED, self.clock,
            gateway=self.gateway, suite=self.suite,
            service=httpx.Client(transport=httpx.MockTransport(self.service.handler)),
        )
        return self.current

    def task(self):
        return self.store.get_task(self.task_id)

    def events(self, kind=None):
        return [event for event in self.store.events_after(self.task_id, 0, 1000) if kind is None or event.type == kind]

    def ops(self):
        with self.store.db.connection() as conn:
            return conn.fetchall("SELECT * FROM operations WHERE task_id = :id ORDER BY started_at", {"id": self.task_id})

    def remote(self):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/office/{self.task_id}"],
            cwd=self.origin.bare, capture_output=True, text=True,
        )
        return result.stdout.strip() or None


def drive(worker: ReleaseWorker, task_id: str, limit: int = 12) -> int:
    for count in range(limit):
        if not worker.step(task_id):
            return count
    raise AssertionError("worker does not settle")


@pytest.fixture
def setup(office, origin, tmp_path, app_key, monkeypatch):
    app = office.workspace_app(origin, tmp_path)
    store = app.state.store
    workspace = app.state.workspace
    client, me = office.login(app=app)
    task_id = office.start_tasks(app, me["id"])[0]
    path = workspace.worktree_path(task_id)
    (path / "apps/chat/web/src/App.tsx").write_text("export function App() {\n  return 'copy button'\n}\n", encoding="utf-8")
    head = workspace.commit(task_id, "Кнопка копирования ответа")
    assert head is not None
    office.set_task(task_id, status="awaiting_integration", spec=json.dumps(SPEC))
    store.add_event(task_id, TaskEventType.role_finished, {"step": "qa", "outcome": "ok", "summary": "QA: тест проходит."},
                    role=AgentRole.qa)
    store.add_event(task_id, TaskEventType.role_finished,
                    {"step": "review", "outcome": "ok", "summary": "Вердикт pass, замечаний: 0."}, role=AgentRole.critic)
    token = secrets.token_hex(16)
    fake = FakeGitHub(origin.bare, token)
    github = github_client("5452356", "174735588", app_key.path, "SlavaPush/vibe-studio",
                           transport=httpx.MockTransport(fake.handler))
    settings = replace(app.state.settings, github_push_url=origin.url, verify_seconds=0)
    pushes = []
    real_push = workspace.push

    def counted_push(*args, **kwargs):
        pushes.append(args)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(workspace, "push", counted_push)
    result = Setup(
        office=office, origin=origin, app=app, store=store, workspace=workspace, client=client, me=me,
        task_id=task_id, head=head, fake=fake, github=github, settings=settings, clock=Clock(), pushes=pushes,
        token=token, current=None, gateway=ScriptedGateway(settings), suite=FakeSuite(store),
        service=Service(origin.main()[:7]), published=origin.main(),
    )
    yield result
    if result.current is not None:
        result.current.shutdown()


def opened(setup) -> ReleaseWorker:
    worker = setup.worker()
    drive(worker, setup.task_id)
    assert setup.task().pr_number == 1
    return worker


def decide(setup, decision, key):
    task = setup.task()
    return setup.client.post(
        f"/api/tasks/{setup.task_id}/decisions",
        json={"decision": decision, "expected_version": task.version, "idempotency_key": key},
    )


def test_task_reaches_open_pr_and_passed_checks(setup):
    worker = setup.worker()
    assert worker.candidates() == [setup.task_id]
    calls = []
    for _ in range(6):
        before = len(setup.fake.mutations()) + len(setup.pushes)
        progressed = worker.step(setup.task_id)
        calls.append(len(setup.fake.mutations()) + len(setup.pushes) - before)
        if not progressed:
            break
    assert max(calls) == 1
    task = setup.task()
    assert setup.remote() == setup.head
    assert (task.status, task.head_sha, task.pr_number) == (TaskStatus.awaiting_integration, setup.head, 1)
    assert task.pr_url == "https://github.com/SlavaPush/vibe-studio/pull/1"
    pr = setup.fake.prs[1]
    assert task.title == MODEL_TITLE
    assert (pr["head"]["ref"], pr["base"]["ref"], pr["title"]) == (f"office/{setup.task_id}", "main", MODEL_TITLE)
    for text in (SPEC["summary"], *SPEC["acceptance_criteria"], "QA: тест проходит.", "Вердикт pass, замечаний: 0."):
        assert text in pr["body"]
    assert "Generated" not in pr["body"] and "Co-Authored" not in pr["body"]
    assert [event.payload for event in setup.events(TaskEventType.pr_opened)] == [
        {"pr_number": 1, "pr_url": task.pr_url}
    ]
    assert task.checks.head_sha == setup.head
    assert {check.status for check in task.checks.checks} == {"missing"}
    assert len(setup.events(TaskEventType.checks_status)) == 1
    assert setup.store.slots_used() == 0

    setup.fake.add_pr_runs(setup.head, 1)
    setup.gateway.handlers["pr_reviewer"] = lambda call: ProviderReply("{}", 10, 2, True)
    assert drive(worker, setup.task_id) == 0
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert [(check.status, check.conclusion) for check in task.checks.checks] == [("completed", "success")] * 4
    assert len(setup.events(TaskEventType.checks_status)) == 2
    assert setup.fake.mutations() == [("POST", "/repos/SlavaPush/vibe-studio/pulls")]
    assert len(setup.pushes) == 1
    assert {row["kind"]: row["status"] for row in setup.ops()} == {
        "push": "succeeded", "create_pr": "succeeded", "pr_review": "failed",
    }


def test_failed_check_closes_pr_and_fails_task(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1, chat_web="failure")
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.checks_failed)
    assert setup.fake.prs[1]["state"] == "closed"
    assert [event.payload for event in setup.events(TaskEventType.pr_closed)] == [{"pr_number": 1}]
    reason = [event for event in setup.events(TaskEventType.role_finished) if event.payload["step"] == "pr_checks"]
    assert "chat-web (failure)" in reason[-1].payload["summary"]
    response = decide(setup, "retry", "retry-checks-failed")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"
    assert worker.candidates() == []


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", "skipped", "neutral"])
def test_non_success_conclusions_fail_checks(setup, conclusion):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1, office_paths=conclusion)
    drive(worker, setup.task_id)
    assert setup.task().failure_kind == FailureKind.checks_failed


def test_runs_of_other_pr_head_attempt_or_app_are_not_counted(setup):
    worker = opened(setup)
    head = setup.head
    setup.fake.add_run("ci.yml", "pull_request", head, [7], {"chat-web": "success"})
    setup.fake.add_run("ci.yml", "pull_request", head, [], {"chat-containers": "success"})
    setup.fake.add_run("ci.yml", "pull_request", "f" * 40, [1], {"chat-web": "success"})
    setup.fake.add_run("ci.yml", "push", head, [1], {"chat-containers": "success"})
    setup.fake.add_run("ci.yml", "pull_request", head, [1], {"chat-web": "success"}, app_id=99)
    setup.fake.add_run("office-paths.yml", "pull_request_target", head, [1], {"office-paths": "success"},
                       run_number=4, run_attempt=1)
    setup.fake.add_run("office-paths.yml", "pull_request_target", head, [1], {"office-paths": None},
                       run_number=4, run_attempt=2)
    summary = {check.name: check.status for check in worker.summary(setup.task()).checks}
    assert summary == {
        "chat-api": "missing", "chat-web": "missing", "chat-containers": "missing", "office-paths": "in_progress"
    }
    setup.fake.add_run("ci.yml", "pull_request", head, [1], {"chat-web": "success"}, run_number=2)
    checks = {check.name: (check.status, check.conclusion) for check in worker.summary(setup.task()).checks}
    assert checks["chat-web"] == ("completed", "success")


def test_missing_runs_after_timeout_need_attention(setup):
    worker = opened(setup)
    setup.clock.offset = timedelta(minutes=29)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=31)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.fake.prs[1]["state"] == "open"


def test_cancel_closes_pr_once(setup):
    worker = opened(setup)
    assert decide(setup, "cancel", "cancel-open-pr").status_code == 200
    assert setup.task().status == TaskStatus.cancelled
    assert worker.candidates() == [setup.task_id]
    drive(worker, setup.task_id)
    assert setup.fake.prs[1]["state"] == "closed"
    assert setup.task().status == TaskStatus.cancelled
    assert len(setup.events(TaskEventType.pr_closed)) == 1
    assert worker.candidates() == []
    assert setup.fake.mutations().count(("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1")) == 1


def test_push_crash_before_call_is_repeated_after_restart(setup, monkeypatch):
    real = setup.workspace.push
    monkeypatch.setattr(setup.workspace, "push", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        setup.worker().step(setup.task_id)
    assert [row["status"] for row in setup.ops()] == ["started"]
    assert setup.remote() is None
    monkeypatch.setattr(setup.workspace, "push", real)
    drive(setup.worker(), setup.task_id)
    assert setup.remote() == setup.head
    row = setup.ops()[0]
    assert (row["status"], row["attempt"]) == ("succeeded", 2)
    assert setup.task().pr_number == 1


def test_push_crash_after_call_is_not_repeated(setup, monkeypatch):
    worker = setup.worker()
    monkeypatch.setattr(worker, "_apply_push", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    assert setup.remote() == setup.head
    assert len(setup.pushes) == 1
    drive(setup.worker(), setup.task_id)
    assert len(setup.pushes) == 1
    assert setup.task().head_sha == setup.head
    assert setup.ops()[0]["attempt"] == 1


def test_lost_push_with_foreign_remote_change_needs_attention(setup, monkeypatch):
    worker = setup.worker()
    monkeypatch.setattr(worker, "_apply_push", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    other = setup.origin.work
    git(other, "fetch", "-q", str(setup.origin.bare), f"office/{setup.task_id}")
    git(other, "checkout", "-q", "-B", "foreign", "FETCH_HEAD")
    setup.origin.write("apps/chat/web/src/Foreign.tsx", "export {}\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "foreign")
    git(other, "push", "-q", "-f", str(setup.origin.bare), f"foreign:office/{setup.task_id}")
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.ops()[0]["status"] == "unknown"
    assert len(setup.pushes) == 1
    assert decide(setup, "retry", "retry-diverged").status_code == 200
    assert setup.task().status == TaskStatus.awaiting_integration
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert len(setup.pushes) == 1


def test_rejected_push_needs_attention(setup):
    other = setup.origin.work
    git(other, "checkout", "-q", "-b", "elsewhere")
    setup.origin.write("apps/chat/web/src/Other.tsx", "export {}\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "other")
    git(other, "push", "-q", str(setup.origin.bare), f"elsewhere:office/{setup.task_id}")
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.ops()[0]["status"] == "failed"
    assert setup.fake.mutations() == []


def test_branch_violation_blocks_push(setup, monkeypatch):
    monkeypatch.setattr(setup.workspace, "verify_branch", lambda *args: ["apps/chat/api/main.py: запрещено"])
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.pushes == []
    assert setup.ops() == []


def test_create_crash_before_call_creates_one_pr(setup, monkeypatch):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    monkeypatch.setattr(worker.github, "create_pr", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    assert setup.fake.prs == {}
    monkeypatch.undo()
    drive(setup.worker(), setup.task_id)
    assert list(setup.fake.prs) == [1]
    assert setup.task().pr_number == 1


def drive_until_pushed(worker, setup):
    while setup.task().head_sha is None:
        assert worker.step(setup.task_id)


def test_create_crash_after_call_finds_pr(setup, monkeypatch):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    monkeypatch.setattr(worker, "_apply_pr", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    assert list(setup.fake.prs) == [1]
    assert setup.task().pr_number is None
    drive(setup.worker(), setup.task_id)
    assert list(setup.fake.prs) == [1]
    assert setup.task().pr_number == 1
    assert setup.fake.mutations().count(("POST", "/repos/SlavaPush/vibe-studio/pulls")) == 1


def test_create_timeout_with_created_pr_does_not_create_second(setup):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    setup.fake.lose_create = True
    assert not worker.step(setup.task_id)
    assert {row["kind"]: row["status"] for row in setup.ops()}["create_pr"] == "started"
    setup.fake.lose_create = False
    drive(worker, setup.task_id)
    assert list(setup.fake.prs) == [1]
    assert setup.task().pr_number == 1
    assert setup.fake.mutations().count(("POST", "/repos/SlavaPush/vibe-studio/pulls")) == 1


def test_failed_result_transaction_keeps_operation_started(setup, monkeypatch):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    real = setup.store.record_event

    def broken(conn, task_id, event_type, payload, **kwargs):
        if event_type == TaskEventType.pr_opened:
            raise RuntimeError("database failure")
        return real(conn, task_id, event_type, payload, **kwargs)

    monkeypatch.setattr(setup.store, "record_event", broken)
    with pytest.raises(RuntimeError):
        worker.step(setup.task_id)
    assert {row["kind"]: row["status"] for row in setup.ops()}["create_pr"] == "started"
    assert setup.task().pr_number is None
    monkeypatch.undo()
    drive(worker, setup.task_id)
    assert setup.task().pr_number == 1
    assert len(setup.fake.prs) == 1


def test_lost_create_then_cancel_and_restart_closes_found_pr(setup, monkeypatch):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    monkeypatch.setattr(worker, "_apply_pr", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    assert decide(setup, "cancel", "cancel-lost-pr").status_code == 200
    restarted = setup.worker()
    assert restarted.candidates() == [setup.task_id]
    drive(restarted, setup.task_id)
    task = setup.task()
    assert (task.status, task.pr_number) == (TaskStatus.cancelled, 1)
    assert setup.fake.prs[1]["state"] == "closed"
    assert restarted.candidates() == []


@pytest.mark.parametrize("moment", ["before", "after"])
def test_close_crash_is_reconciled_after_restart(setup, monkeypatch, moment):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1, chat_web="failure")
    if moment == "before":
        monkeypatch.setattr(worker.github, "close_pr", lambda *args: (_ for _ in ()).throw(Crash()))
    else:
        monkeypatch.setattr(worker, "_apply_close", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.step(setup.task_id)
    assert setup.fake.prs[1]["state"] == ("open" if moment == "before" else "closed")
    monkeypatch.undo()
    drive(setup.worker(), setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.checks_failed)
    assert setup.fake.prs[1]["state"] == "closed"
    patches = setup.fake.mutations().count(("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1"))
    assert patches == 1
    close = [row for row in setup.ops() if row["kind"] == "close_pr"][0]
    assert (close["status"], close["attempt"]) == ("succeeded", 2 if moment == "before" else 1)


def test_unavailable_github_reaches_reconcile_limit(setup):
    worker = setup.worker()
    drive_until_pushed(worker, setup)
    setup.fake.lose_create = True
    worker.step(setup.task_id)
    setup.fake.down = True
    for _ in range(3):
        assert not worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=31)
    worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert {row["kind"]: row["status"] for row in setup.ops()}["create_pr"] == "unknown"
    setup.fake.down = False
    setup.clock.offset = timedelta()
    assert decide(setup, "retry", "retry-after-outage").status_code == 200
    assert setup.task().status == TaskStatus.awaiting_integration
    drive(worker, setup.task_id)
    assert setup.task().pr_number == 1
    assert len(setup.fake.prs) == 1


def test_unavailable_github_for_cancelled_task_records_unknown_and_stops(setup):
    worker = opened(setup)
    assert decide(setup, "cancel", "cancel-outage").status_code == 200
    setup.fake.down = True
    assert not worker.step(setup.task_id)
    assert not worker.step(setup.task_id)
    close =[row for row in setup.ops() if row["kind"] == "close_pr"][0]
    assert close["status"] == "started"
    setup.clock.offset = timedelta(minutes=31)
    worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.cancelled
    unknown = setup.events(TaskEventType.operation_unknown)
    assert [(event.payload["kind"], event.payload["pr_number"]) for event in unknown] == [("close_pr", 1)]
    assert worker.candidates() == []


def test_retry_routes_by_operation_log(setup):
    worker = opened(setup)
    setup.office.set_task(setup.task_id, status="needs_attention")
    assert decide(setup, "retry", "retry-with-pr").status_code == 200
    assert setup.task().status == TaskStatus.awaiting_integration
    assert worker.candidates() == [setup.task_id]


def test_operation_log_states(setup):
    log = OperationLog(setup.store.db)
    first, allowed = log.begin("push", "push:k", setup.task_id)
    assert (first.status, first.attempt, allowed) == (OpStatus.started, 1, True)
    again, allowed = log.begin("push", "push:k", setup.task_id)
    assert (again.id, allowed) == (first.id, False)
    log.mark(first, OpStatus.failed, detail="not found")
    retried, allowed = log.begin("push", "push:k", setup.task_id)
    assert (retried.status, retried.attempt, allowed) == (OpStatus.started, 2, True)
    log.mark(retried, OpStatus.succeeded)
    done, allowed = log.begin("push", "push:k", setup.task_id)
    assert (done.status, allowed) == (OpStatus.succeeded, False)
    log.mark(done, OpStatus.unknown)
    assert log.begin("push", "push:k", setup.task_id)[1] is False


def test_secrets_do_not_leak(setup, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []
    real_run = subprocess.run

    def recording(args, **kwargs):
        calls.append((list(args), dict(kwargs.get("env") or {})))
        return real_run(args, **kwargs)

    monkeypatch.setattr(workspace_module.subprocess, "run", recording)
    worker = setup.worker()
    drive(worker, setup.task_id)
    setup.fake.status[("GET", "/repos/SlavaPush/vibe-studio/actions/runs")] = 500
    errors = []
    assert not worker.step(setup.task_id)
    with pytest.raises(GitHubError) as failed:
        worker.github.runs(setup.head)
    errors.append(failed.value)
    worker.run_once()
    setup.fake.status.clear()
    setup.fake.down = True
    assert not worker.step(setup.task_id)
    with pytest.raises(GitHubUnavailable) as unavailable:
        worker.github.get_pr(1)
    errors.append(unavailable.value)
    worker.run_once()
    jwt_value = setup.fake.jwts[0]
    basic = base64.b64encode(f"x-access-token:{setup.token}".encode()).decode()
    secrets_list = [setup.token, jwt_value, basic]
    push_calls = [call for call in calls if call[0][:2] == ["git", "push"]]
    assert len(push_calls) == 1
    assert push_calls[0][1]["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
    assert push_calls[0][1]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    for call in calls:
        args, env = call
        for secret in secrets_list:
            assert all(secret not in arg for arg in args)
            if call is not push_calls[0]:
                assert all(secret not in value for value in env.values())
    config = (setup.workspace.clone / ".git" / "config").read_text()
    with setup.store.db.connection() as conn:
        rows = [conn.fetchall(f"SELECT * FROM {table}") for table in TABLES]
    texts = [config, caplog.text, json.dumps(rows, default=str)]
    texts += [json.dumps(event.payload, ensure_ascii=False) for event in setup.events()]
    for error in errors:
        assert error.__cause__ is None and error.__context__ is None
        texts.append(f"{error} {error!r}")
    for text in texts:
        for secret in secrets_list:
            assert secret not in text


def test_push_to_bare_repository_keeps_token_out_of_clone(setup):
    result = setup.workspace.push(setup.task_id, setup.origin.url, setup.token, setup.head)
    assert result == PushResult.ok
    assert setup.remote() == setup.head
    assert setup.token not in (setup.workspace.clone / ".git" / "config").read_text()
    found = subprocess.run(
        ["git", "config", "--get-regexp", "extraheader"],
        cwd=setup.workspace.worktree_path(setup.task_id), capture_output=True, text=True,
    )
    assert found.stdout == ""


def test_worktree_survives_background_preparation_after_gate(setup):
    path = setup.workspace.worktree_path(setup.task_id)
    setup.workspace.ensure_worktree(setup.task_id)
    assert setup.workspace.is_task_worktree(path, setup.task_id)
    setup.workspace.recover()
    setup.workspace.wait_idle()
    assert setup.workspace.is_task_worktree(path, setup.task_id)


def test_create_app_without_github_app_warns_once(office, origin, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="office")
    app = office.new_app(workspace=True, runner=True, target_repo_url=origin.url, workspace_dir=tmp_path / "ws")
    assert app.state.release is None
    warnings = [record for record in caplog.records if "GitHub App" in record.getMessage()]
    assert len(warnings) == 1
    client, _ = office.login(app=app)
    assert client.get("/api/slots").status_code == 200


def test_worker_starts_after_worktree_recovery(office, origin, tmp_path, app_key, monkeypatch):
    gate = threading.Event()
    order = []
    real = Workspace._recover

    def delayed(self):
        gate.wait(10)
        real(self)
        order.append("recovered")

    monkeypatch.setattr(Workspace, "_recover", delayed)
    monkeypatch.setattr(ReleaseWorker, "start", lambda self: order.append(("started", self)))
    app = office.new_app(
        workspace=True, runner=True, target_repo_url=origin.url, workspace_dir=tmp_path / "ws",
        github_app_id="1", github_app_installation_id="2", github_app_private_key_path=app_key.path,
    )
    assert app.state.release is not None
    time.sleep(0.2)
    assert order == []
    gate.set()
    app.state.workspace.wait_idle()
    assert order == ["recovered", ("started", app.state.release)]


def test_foreign_push_to_pr_branch_needs_attention(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1, chat_web="failure")
    other = setup.origin.work
    git(other, "fetch", "-q", str(setup.origin.bare), f"office/{setup.task_id}")
    git(other, "checkout", "-q", "-B", "foreign", "FETCH_HEAD")
    setup.origin.write("apps/chat/web/src/Foreign.tsx", "export {}\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "foreign")
    git(other, "push", "-q", str(setup.origin.bare), f"foreign:office/{setup.task_id}")
    before = setup.task().checks
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert task.checks == before
    assert setup.fake.prs[1]["state"] == "open"
    assert ("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1") not in setup.fake.mutations()


def test_pr_closed_outside_office_needs_attention(setup):
    worker = opened(setup)
    setup.fake.prs[1]["state"] = "closed"
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention


@pytest.mark.parametrize("failure", ["forbidden", "down"])
def test_unreadable_checks_need_attention_after_timeout(setup, failure):
    worker = opened(setup)
    if failure == "down":
        setup.fake.down = True
    else:
        setup.fake.status[("GET", "/repos/SlavaPush/vibe-studio/actions/runs")] = 403
    assert not worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=31)
    assert not worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention


def test_cancel_after_unknown_close_closes_pr(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1, chat_web="failure")
    setup.fake.status[("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1")] = 403
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert [row["status"] for row in setup.ops() if row["kind"] == "close_pr"] == ["unknown"]
    setup.fake.status.clear()
    assert decide(setup, "cancel", "cancel-after-unknown").status_code == 200
    assert worker.candidates() == [setup.task_id]
    drive(worker, setup.task_id)
    assert setup.fake.prs[1]["state"] == "closed"
    assert setup.task().status == TaskStatus.cancelled
    assert len(setup.events(TaskEventType.pr_closed)) == 1
    assert setup.events(TaskEventType.operation_unknown) == []
    assert worker.candidates() == []


def test_cancel_during_push_reconcile_still_closes_pr(setup, monkeypatch):
    worker = opened(setup)
    log = OperationLog(setup.store.db)
    log.begin("push", f"push:{setup.task_id}:{'e' * 40}", setup.task_id)
    real = setup.github.branch_sha

    def cancel_during_read(branch):
        sha = real(branch)
        assert decide(setup, "cancel", "cancel-during-reconcile").status_code == 200
        return sha

    monkeypatch.setattr(setup.github, "branch_sha", cancel_during_read)
    assert not worker.step(setup.task_id)
    assert setup.task().status == TaskStatus.cancelled
    assert [row["status"] for row in setup.ops() if row["kind"] == "push"] == ["succeeded", "unknown"]
    assert setup.events(TaskEventType.operation_unknown) == []
    monkeypatch.setattr(setup.github, "branch_sha", real)
    assert worker.candidates() == [setup.task_id]
    drive(worker, setup.task_id)
    assert setup.fake.prs[1]["state"] == "closed"
    assert worker.candidates() == []


def test_unknown_push_does_not_block_closing_known_pr(setup):
    worker = opened(setup)
    log = OperationLog(setup.store.db)
    operation, _ = log.begin("push", f"push:{setup.task_id}:{'e' * 40}", setup.task_id)
    log.mark(operation, OpStatus.unknown, detail="diverged")
    setup.office.set_task(setup.task_id, status="needs_attention")
    assert decide(setup, "cancel", "cancel-unknown-push").status_code == 200
    drive(worker, setup.task_id)
    assert setup.fake.prs[1]["state"] == "closed"
    assert worker.candidates() == []


def merged(setup) -> ReleaseWorker:
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    drive(worker, setup.task_id)
    assert setup.task().merge_sha == setup.origin.main()
    return worker


def candidates_of(setup):
    return [release for release in setup.store.list_releases() if release.task_id == setup.task_id]


def push_foreign(setup):
    other = setup.origin.work
    git(other, "fetch", "-q", str(setup.origin.bare), f"office/{setup.task_id}")
    git(other, "checkout", "-q", "-B", "foreign", "FETCH_HEAD")
    setup.origin.write("apps/chat/web/src/Foreign.tsx", "export {}\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "foreign")
    git(other, "push", "-q", str(setup.origin.bare), f"foreign:office/{setup.task_id}")


def test_approved_pr_is_merged_checked_and_becomes_candidate(setup):
    worker = merged(setup)
    task = setup.task()
    sha = task.merge_sha
    [put] = [request for request in setup.fake.requests if request.method == "PUT"]
    assert put.url.path == "/repos/SlavaPush/vibe-studio/pulls/1/merge"
    assert json.loads(put.content) == {"sha": setup.head, "merge_method": "squash", "commit_title": f"{MODEL_TITLE} (#1)"}
    assert git(setup.origin.bare, "rev-parse", "main~1") == setup.published
    assert [event.payload for event in setup.events(TaskEventType.pr_merged)] == [{"pr_number": 1, "merge_sha": sha}]
    assert task.status == TaskStatus.awaiting_integration
    assert task.checks.head_sha == sha
    assert {check.status for check in task.checks.checks} == {"missing"}
    assert setup.suite.calls == []
    run_id = setup.fake.add_push_runs(sha)
    setup.fake.add_run("ci.yml", "push", sha, [], {"chat-web": "failure"}, head_branch="office/other")
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.awaiting_release
    assert task.checks.head_sha == sha
    assert [check.name for check in task.checks.checks] == [name for name in CI_JOBS]
    assert setup.suite.calls == [(setup.task_id, sha, sha, True)]
    [candidate] = candidates_of(setup)
    assert (candidate.kind, candidate.status, candidate.commit_sha) == ("release", "pending", sha)
    assert candidate.expected_previous_sha == setup.published
    assert (candidate.spec_hash, candidate.suite_hash) == (SPEC["spec_hash"], "sha256:fake")
    assert candidate.ci_run_ids == [str(run_id)]
    assert candidate.pr_url == task.pr_url
    assert candidate.pr_review_id == task.pr_review.id
    assert candidate.e2e.commit_sha == sha
    assert [check.command for check in candidate.e2e.checks][-1].startswith("playwright test")
    assert len(setup.service.requests) == 1
    assert setup.events(TaskEventType.release_candidate)[-1].payload == {"release_id": candidate.id, "commit_sha": sha}
    setup.office.set_task(setup.task_id, status="awaiting_integration")
    drive(setup.worker(), setup.task_id)
    assert len(candidates_of(setup)) == 1
    assert len(setup.suite.calls) == 1
    assert len(setup.gateway.role_calls("pr_reviewer")) == 1
    assert setup.fake.merges() == 1


def test_new_head_after_review_is_not_merged(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    real = setup.gateway.handlers["pr_reviewer"]

    def review_then_push(call):
        push_foreign(setup)
        return real(call)

    setup.gateway.handlers["pr_reviewer"] = review_then_push
    assert worker.step(setup.task_id)
    review = setup.task().pr_review
    assert (review.verdict, review.head_sha) == ("approve", setup.head)
    assert not worker._merge(task_carrier(setup.task()), review)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert task.pr_review == review
    assert setup.fake.merges() == 0
    assert setup.fake.prs[1]["state"] == "open"


def test_new_head_during_refused_merge_needs_attention(setup, monkeypatch):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    real = worker.github.merge_pr

    def push_then_merge(*args):
        push_foreign(setup)
        return real(*args)

    monkeypatch.setattr(worker.github, "merge_pr", push_then_merge)
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert task.merge_sha is None
    assert setup.fake.merges() == 1
    assert setup.fake.prs[1]["merged"] is False
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["failed"]
    reason = [event for event in setup.events(TaskEventType.role_finished) if event.payload["step"] == "merge_pr"]
    assert "решение недействительно" in reason[-1].payload["summary"].lower()


@pytest.mark.parametrize("status", [405, 409, 422])
def test_refused_merge_needs_attention_and_retry_merges(setup, status):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.status[("PUT", "/repos/SlavaPush/vibe-studio/pulls/1/merge")] = status
    drive(worker, setup.task_id)
    task = setup.task()
    assert task.status == TaskStatus.needs_attention
    assert setup.fake.prs[1]["state"] == "open"
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["failed"]
    setup.fake.status.clear()
    assert decide(setup, "retry", f"retry-merge-{status}").status_code == 200
    drive(worker, setup.task_id)
    assert setup.task().merge_sha == setup.origin.main()
    assert setup.fake.merges() == 2
    assert len(setup.gateway.role_calls("pr_reviewer")) == 1


def test_merge_timeout_is_reconciled_without_second_put(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.hang_merge = True
    drive(worker, setup.task_id)
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["started"]
    setup.fake.hang_merge = False
    assert not worker.step(setup.task_id)
    assert setup.task().merge_sha is None
    sha = setup.fake.squash(setup.fake.prs[1], setup.head)
    assert worker.step(setup.task_id)
    assert setup.task().merge_sha == sha
    assert setup.fake.merges() == 1
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["succeeded"]


def test_unreadable_merge_answer_is_reconciled_without_second_put(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.garble_merge = True
    drive(worker, setup.task_id)
    setup.fake.garble_merge = False
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["started"]
    assert setup.task().status == TaskStatus.awaiting_integration
    assert not worker.step(setup.task_id)
    sha = setup.fake.squash(setup.fake.prs[1], setup.head)
    drive(worker, setup.task_id)
    assert setup.task().merge_sha == sha
    assert setup.fake.merges() == 1


def test_cancel_accepted_before_merge_operation_prevents_put(setup, monkeypatch):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    real = setup.store.load_review
    cancelled = []

    def cancel_after_read(conn, task_id, head_sha):
        review = real(conn, task_id, head_sha)
        if review is not None and review.verdict == "approve" and not cancelled:
            cancelled.append(True)
            assert decide(setup, "cancel", "cancel-before-merge").status_code == 200
        return review

    monkeypatch.setattr(setup.store, "load_review", cancel_after_read)
    drive(worker, setup.task_id)
    monkeypatch.undo()
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.merge_sha) == (TaskStatus.cancelled, None)
    assert setup.fake.merges() == 0
    assert [row["kind"] for row in setup.ops() if row["kind"] == "merge_pr"] == []
    assert setup.fake.prs[1]["state"] == "closed"


def test_lost_merge_answer_is_adopted_by_reconcile(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.lose_merge = True
    drive(worker, setup.task_id)
    assert setup.task().merge_sha is None
    setup.fake.lose_merge = False
    drive(worker, setup.task_id)
    assert setup.task().merge_sha == setup.origin.main()
    assert setup.fake.merges() == 1


def test_unknown_merge_after_limit_needs_attention_and_retry_only_reconciles(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.hang_merge = True
    drive(worker, setup.task_id)
    setup.fake.hang_merge = False
    setup.clock.offset = timedelta(minutes=31)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert [row["status"] for row in setup.ops() if row["kind"] == "merge_pr"] == ["unknown"]
    response = decide(setup, "cancel", "cancel-unknown-merge")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"
    setup.clock.offset = timedelta()
    assert decide(setup, "retry", "retry-unknown-merge").status_code == 200
    drive(worker, setup.task_id)
    assert setup.task().merge_sha is None
    assert setup.fake.merges() == 1
    sha = setup.fake.squash(setup.fake.prs[1], setup.head)
    drive(worker, setup.task_id)
    assert setup.task().merge_sha == sha
    assert setup.fake.merges() == 1


def test_merge_crash_after_put_is_reconciled_after_restart(setup, monkeypatch):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    monkeypatch.setattr(worker, "_apply_merge", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        drive(worker, setup.task_id)
    assert setup.task().merge_sha is None
    monkeypatch.undo()
    drive(setup.worker(), setup.task_id)
    assert setup.task().merge_sha == setup.origin.main()
    assert setup.fake.merges() == 1


def test_cancel_is_forbidden_during_and_after_merge(setup):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    setup.fake.hang_merge = True
    drive(worker, setup.task_id)
    body = setup.client.get(f"/api/tasks/{setup.task_id}").json()
    assert (body["merge_pending"], body["merge_sha"], body["integration_started"]) == (True, None, True)
    assert [event.payload for event in setup.events(TaskEventType.merge_started)] == [
        {"pr_number": 1, "head_sha": setup.head}
    ]
    response = decide(setup, "cancel", "cancel-during-merge")
    assert response.status_code == 409
    assert "слит" in response.json()["message"]
    setup.fake.squash(setup.fake.prs[1], setup.head)
    setup.fake.hang_merge = False
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_integration
    assert setup.task().merge_pending is False
    assert decide(setup, "cancel", "cancel-waiting-push-checks").status_code == 409
    setup.clock.offset = timedelta(minutes=31)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert decide(setup, "cancel", "cancel-after-merge").status_code == 409
    assert setup.task().status == TaskStatus.needs_attention
    assert setup.fake.mutations().count(("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1")) == 0


def test_cancel_racing_merge_keeps_pr_and_records_merge(setup, monkeypatch):
    worker = opened(setup)
    setup.fake.add_pr_runs(setup.head, 1)
    real = worker.github.merge_pr

    def merge_after_cancel(*args):
        setup.office.set_task(setup.task_id, status="cancelled")
        return real(*args)

    monkeypatch.setattr(worker.github, "merge_pr", merge_after_cancel)
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.merge_sha) == (TaskStatus.cancelled, setup.origin.main())
    assert worker.candidates() == []
    assert setup.fake.mutations().count(("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1")) == 0


def blocked_by_checks(setup):
    [candidate] = candidates_of(setup)
    sha = setup.origin.main()
    assert (candidate.status, candidate.commit_sha, candidate.expected_previous_sha) == ("failed", sha, setup.published)
    with setup.store.db.connection() as conn:
        assert setup.store.queue_state(conn)["release_blocked_by"] == candidate.id
    [target] = candidate.revert.targets
    assert (target.source, target.base_sha, target.head_sha) == ("checks_failed", setup.published, sha)
    assert candidate.revert.status == "pending"
    assert setup.task().rework_reason == candidate.blocked_reason
    return candidate


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_failed_push_check_of_merge_commit_fails_and_failure_blocks_for_revert(setup, conclusion):
    worker = merged(setup)
    run_id = setup.fake.add_push_runs(setup.task().merge_sha, chat_web=conclusion)
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.checks_failed)
    assert setup.suite.calls == []
    assert decide(setup, "retry", "retry-merge-checks").status_code == 409
    if conclusion != "failure":
        assert candidates_of(setup) == []
        return
    candidate = blocked_by_checks(setup)
    assert candidate.ci_run_ids == [str(run_id)]
    assert "chat-web (failure)" in candidate.blocked_reason
    drive(setup.worker(), setup.task_id)
    assert len(candidates_of(setup)) == 1


def test_merge_commit_checks_wait_then_need_attention(setup):
    worker = merged(setup)
    setup.fake.add_run("ci.yml", "pull_request", setup.task().merge_sha, [1], {name: "success" for name in CI_JOBS})
    drive(worker, setup.task_id)
    assert {check.status for check in setup.task().checks.checks} == {"missing"}
    assert setup.task().status == TaskStatus.awaiting_integration
    setup.clock.offset = timedelta(minutes=31)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert candidates_of(setup) == []


@pytest.mark.parametrize("step", ["lint", "e2e"])
def test_failed_critical_e2e_fails_and_blocks_for_revert(setup, step):
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    setup.suite.exits = lambda task_id, app, qa, e2e: {step: 1}
    drive(worker, setup.task_id)
    task = setup.task()
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.checks_failed)
    candidate = blocked_by_checks(setup)
    assert [check.exit_code for check in candidate.e2e.checks if check.exit_code] == [1]
    reason = [event for event in setup.events(TaskEventType.role_finished) if event.payload["step"] == "merge_checks"]
    assert "exit 1" in reason[-1].payload["summary"]


def test_interrupted_suite_is_repeated_and_complete_report_is_reused(setup, monkeypatch):
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)

    def crash(*args):
        raise Crash()

    setup.suite.on_suite = crash
    with pytest.raises(Crash):
        drive(worker, setup.task_id)
    setup.suite.on_suite = None
    partial = CheckReport(
        task_id=setup.task_id, commit_sha=setup.task().merge_sha, spec_hash=None, suite_hash="sha256:fake",
        checks=[CheckResult(command="npm run lint", exit_code=0, duration_ms=1, artifact_urls=[])],
    )
    setup.store.add_check_report(partial)
    restarted = setup.worker()
    monkeypatch.setattr(restarted, "_candidate", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        drive(restarted, setup.task_id)
    assert len(setup.suite.calls) == 2
    monkeypatch.undo()
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release
    assert len(setup.suite.calls) == 2
    assert len(candidates_of(setup)) == 1


def test_sandbox_error_needs_attention(setup):
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)

    def broken(*args):
        raise SandboxError("Sandbox недоступен")

    setup.suite.on_suite = broken
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert candidates_of(setup) == []


def test_restart_after_merge_continues_with_merge_commit(setup):
    merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    drive(setup.worker(), setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release


@pytest.mark.parametrize("failure", ["down", "status", "body"])
def test_unavailable_service_version_needs_attention(setup, failure):
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    if failure == "down":
        setup.service.down = True
    elif failure == "status":
        setup.service.status = 503
    else:
        setup.service.version = "dev"
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert candidates_of(setup) == []
    setup.service.down, setup.service.status, setup.service.version = False, 200, setup.published[:7]
    assert decide(setup, "retry", f"retry-service-{failure}").status_code == 200
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release
    assert len(setup.suite.calls) == 1


def test_sleeping_service_wakes_within_window_and_candidate_is_created(setup, monkeypatch):
    monkeypatch.setattr(release_queue, "VERIFY_INTERVAL", 0)
    setup.settings = replace(setup.settings, verify_seconds=120)
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    setup.service.timeouts = 2
    before = len(setup.service.requests)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release
    assert len(candidates_of(setup)) == 1
    versions = [r for r in setup.service.requests[before:] if r.url.path == "/api/version"]
    assert len(versions) >= 3
    assert all(r.extensions["timeout"]["read"] == SERVICE_TIMEOUT for r in versions[:3])


def test_service_version_window_is_exhausted_then_needs_attention(setup, monkeypatch):
    monkeypatch.setattr(release_queue, "VERIFY_INTERVAL", 0)
    setup.settings = replace(setup.settings, verify_seconds=120)
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    setup.service.timeouts = 1000
    setup.service.clock, setup.service.tick = setup.clock, timedelta(seconds=30)
    before = len(setup.service.requests)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert candidates_of(setup) == []
    timeouts = [r.extensions["timeout"]["read"] for r in setup.service.requests[before:]]
    assert 3 <= len(timeouts) <= 6
    assert timeouts[0] == SERVICE_TIMEOUT
    assert timeouts[-1] < SERVICE_TIMEOUT


def test_published_candidate_is_base_and_external_version_replaces_it(setup):
    other = setup.office.queued_task(setup.me["id"])
    setup.office.add_candidate(other, commit_sha="d" * 40, status=ReleaseStatus.released, is_published=True)
    setup.service.version = "d" * 7
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    drive(worker, setup.task_id)
    assert candidates_of(setup)[0].expected_previous_sha == "d" * 40
    setup.office.set_task(setup.task_id, status="awaiting_integration")
    with setup.store.db.transaction() as conn:
        conn.execute("DELETE FROM release_candidates WHERE task_id = :id", {"id": setup.task_id})
    setup.service.version = setup.published[:7]
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release
    [candidate] = candidates_of(setup)
    assert candidate.expected_previous_sha == setup.published
    assert not any(release.is_published for release in setup.store.list_releases())
    with setup.store.db.connection() as conn:
        assert setup.store.queue_state(conn)["published_sha"] == setup.published


@pytest.mark.parametrize("case", ["unknown", "outside_main"])
def test_initial_base_must_resolve_into_main_history(setup, case):
    if case == "unknown":
        setup.service.version = "0" * 7
    else:
        setup.origin.write("apps/chat/web/src/Side.tsx", "export {}\n")
        git(setup.origin.work, "checkout", "-q", "-b", "side")
        git(setup.origin.work, "add", "-A")
        git(setup.origin.work, "commit", "-q", "-m", "side")
        git(setup.origin.work, "push", "-q", str(setup.origin.bare), "side")
        setup.service.version = git(setup.origin.bare, "rev-parse", "side")[:7]
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.needs_attention
    assert candidates_of(setup) == []


def test_candidate_and_status_change_are_one_transaction(setup, monkeypatch):
    worker = merged(setup)
    setup.fake.add_push_runs(setup.task().merge_sha)
    real = setup.store.transition

    def broken(conn, task_id, to, **kwargs):
        if to == TaskStatus.awaiting_release:
            raise RuntimeError("database failure")
        return real(conn, task_id, to, **kwargs)

    monkeypatch.setattr(setup.store, "transition", broken)
    with pytest.raises(RuntimeError):
        drive(worker, setup.task_id)
    assert candidates_of(setup) == []
    assert setup.task().status == TaskStatus.awaiting_integration
    monkeypatch.undo()
    drive(worker, setup.task_id)
    assert setup.task().status == TaskStatus.awaiting_release
    assert len(candidates_of(setup)) == 1
    assert len(setup.suite.calls) == 1


def test_candidate_requirements_and_suite_are_not_replaced(setup):
    worker = merged(setup)
    sha = setup.task().merge_sha
    setup.fake.add_push_runs(sha)
    drive(worker, setup.task_id)
    [candidate] = candidates_of(setup)
    assert (candidate.commit_sha, candidate.spec_hash, candidate.suite_hash) == (sha, SPEC["spec_hash"], "sha256:fake")
    other = SPEC | {"spec_hash": "sha256:other", "summary": "Другие требования."}
    assert setup.store.publish_spec(setup.task_id, other, None) is None
    response = decide(setup, "approve_spec", "approve-after-candidate")
    assert (response.status_code, response.json()["code"]) == (409, "decision_not_allowed")
    assert setup.task().spec.spec_hash == SPEC["spec_hash"]
    setup.store.add_check_report(CheckReport(
        task_id=setup.task_id, commit_sha=sha, spec_hash="sha256:other", suite_hash="sha256:other",
        checks=[CheckResult(command="npm run lint", exit_code=0, duration_ms=1, artifact_urls=[])],
    ))
    drive(setup.worker(), setup.task_id)
    [same] = candidates_of(setup)
    assert (same.id, same.commit_sha, same.spec_hash, same.suite_hash) == (
        candidate.id, sha, SPEC["spec_hash"], "sha256:fake"
    )
    assert same.e2e == candidate.e2e
    assert setup.task().status == TaskStatus.awaiting_release


def test_report_of_other_suite_does_not_admit_candidate(setup):
    worker = merged(setup)
    sha = setup.task().merge_sha
    setup.fake.add_push_runs(sha)
    setup.store.add_check_report(CheckReport(
        task_id=setup.task_id, commit_sha=sha, spec_hash=SPEC["spec_hash"], suite_hash="sha256:old",
        checks=[
            CheckResult(command=command, exit_code=0, duration_ms=1, artifact_urls=[])
            for command in ("npm run lint", "npm run test", "npm run build")
        ],
    ))
    drive(worker, setup.task_id)
    assert setup.suite.calls == [(setup.task_id, sha, sha, True)]
    [candidate] = candidates_of(setup)
    assert candidate.suite_hash == candidate.e2e.suite_hash == "sha256:fake"
    assert candidate.e2e.checks[-1].command.startswith("playwright test")
