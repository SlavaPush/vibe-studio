import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import ORIGIN_FILES, UI_TASK, FakeGitHub, ScriptedGateway, git, wait_for
from test_graph import approve, create, events, transitions, wait_status

from app.branch_check import WorkspaceError
from app.config import CHECKOUT_ROOT, Settings
from app.db import to_text, utcnow
from app.github import github_client
from app.operations import OperationLog, OpStatus
from app.release import ReleaseWorker
from app.render import render_client
from app.required_checks import load_required_checks
from app.rework import HIDDEN, SOURCE_LIMIT, ReworkMaterials, clip, sanitize, secrets_of
from app.schemas import (
    ChangeClass,
    DeployResult,
    ModelProfile,
    ReleaseKind,
    ReleaseStatus,
    RollbackResult,
    TaskEventType,
    TaskStatus,
    VerifyResult,
)
from app.tasks import IntakeOutcome
from app.workspace import rework_key

APP = "apps/chat/web/src/App.tsx"
ORIGINAL = ORIGIN_FILES[APP]
CHANGED = "export function App() {\n  return 'copy button'\n}\n"
ANT, PROJ, SUPABASE = "sk-" + "ant-", "sk-" + "proj-", "sb_" + "secret_"
FAKE_KEY = f"{ANT}abcdefgh12345678"
REQUIRED = load_required_checks(CHECKOUT_ROOT)


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("ключ модели sk-test-anthropic-value-123", "ключ модели "),
        ("Authorization: Bearer abc.def.ghi-long-value", "Authorization: "),
        ("proxy-authorization=Basic dXNlcjpwYXNzd29yZA==", "proxy-authorization="),
        ("Cookie: office_session=abcdef123456", "Cookie: "),
        ("set-cookie: session=abcdef123456; HttpOnly", "set-cookie: "),
        ("заголовок token ghs_abcdefgh12345678", "заголовок token "),
        ("jwt eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMjMifQ.c2lnbmF0dXJlLXZhbHVl", "jwt "),
        (f"ключи {ANT}api03-abcdefgh {PROJ}abcdefgh12 {SUPABASE}abcdefgh12", "ключи "),
        ("ghp_abcdefgh12345678 github_pat_abcdefgh1234 rnd_abcdefgh12345678 sb_publishable_abcdefgh12", ""),
        ("api key: 0123456789abcdef0123456789abcdef01", "api key: "),
        ("postgresql://office:pa55word@db.example.org:5432/office", "postgresql://"),
        ("GET /hook?token=abcdef&sig=xyz&page=2", "GET /hook?token="),
        ("DB_PASSWORD=hunter22 access_code: 1234-5678", "DB_PASSWORD="),
        ('{"password":"hunter22","user":"Иван"}', '{"password":'),
        ('{"Cookie":"session=abcdef", "Authorization": "token abcdef"}', '{"Cookie":'),
        ("{'access_code': '1234-5678', 'cookie': 'session=abcdef', 'apiKey': 'abcdef'}", "{'access_code': "),
        ('{"db_passwd": "hunter22"}', '{"db_passwd": '),
        ("письмо от maria@example.org", "письмо от "),
        ("клиент 10.237.24.148:43252", "клиент "),
        ("адрес 2001:0db8:85a3:0000:0000:8a2e:0370:7334 и fe80::1ff:fe23:4567:890a", "адрес "),
    ],
)
def test_sanitize_hides_secrets_and_personal_data(text, kept):
    secrets = secrets_of(replace_settings(anthropic_api_key="sk-test-anthropic-value-123"))
    result = sanitize(text, secrets)
    assert HIDDEN in result
    assert result.startswith(kept)
    for word in ("abcdef", "pa55word", "hunter22", "maria@", "10.237", "0db8", "fe80", "dXNlcjpw", "eyJ", "1234-5678"):
        assert word not in result


def replace_settings(**fields):
    return replace(Settings(), **fields)


def test_sanitize_keeps_times_and_plain_code():
    text = "2026-10-25T05:01:03Z ERROR render failed in App.tsx line 12"
    assert sanitize(text) == text


def test_secrets_of_settings_include_database_password():
    settings = replace_settings(
        openai_api_key="sk-openai-value-1", render_api_key="rnd_render_value", database_url="postgresql://u:dbpass123@h/db"
    )
    found = secrets_of(settings, ["ghs_token_value_1"])
    assert {"sk-openai-value-1", "rnd_render_value", "dbpass123", "ghs_token_value_1"} <= set(found)
    assert sanitize("x dbpass123 y", found) == f"x {HIDDEN} y"


def test_clip_limits_each_source_to_four_kilobytes():
    text = "строка лога\n" * 2000
    assert len(clip(text).encode("utf-8")) <= SOURCE_LIMIT
    assert len(clip(text, tail=True).encode("utf-8")) <= SOURCE_LIMIT
    assert clip("short") == "short"


def render_handler(seen):
    sample = {
        "hasMore": False,
        "logs": [
            {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "info"}],
             "message": 'INFO:     10.237.24.148:43252 - "GET /healthz HTTP/1.1" 200 OK', "timestamp": "t1"},
            {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "info"}],
             "message": "сообщение участника: мой пароль", "timestamp": "t2"},
            {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "error"}],
             "message": f"Exception in ASGI application from 10.1.2.3 key={FAKE_KEY}", "timestamp": "t3"},
            {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "info"}],
             "message": "Traceback (most recent call last):", "timestamp": "t4"},
            {"labels": [{"name": "type", "value": "build"}, {"name": "level", "value": "info"}],
             "message": "npm run build", "timestamp": "t5"},
        ],
    }

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path == "/v1/services/srv-test":
            return httpx.Response(200, json={"id": "srv-test", "ownerId": "tea-owner"})
        if path == "/v1/logs":
            return httpx.Response(200, json=sample)
        if path.startswith("/v1/services/srv-test/deploys/"):
            deploy_id = path.rsplit("/", 1)[1]
            trigger = "rollback" if deploy_id == "dep-3" else "api"
            return httpx.Response(200, json={"id": deploy_id, "status": "live", "trigger": trigger,
                                             "createdAt": "2026-10-25T05:00:00Z", "finishedAt": None})
        return httpx.Response(404, json={})

    return handler


def failed_release(office, task_id, *, deployed=True, run_ids=None):
    release = office.add_candidate(task_id, status=ReleaseStatus.failed, ci_run_ids=run_ids or [])
    now = utcnow()
    with office.store.db.locked_transaction() as conn:
        fields = {}
        if deployed:
            fields = {
                "deploy": DeployResult(render_deploy_id="dep-2", render_status="live", started_at=now - timedelta(minutes=5),
                                       finished_at=now - timedelta(minutes=4), published_sha=None, error=None),
                "verify": VerifyResult(expected_sha="c" * 40, ok=False, fault=False, attempts=3, health=False, version=None,
                                       page_version=None, error="/healthz не ответил status ok", checked_at=now - timedelta(minutes=3)),
                "rollback": RollbackResult(target_sha="a" * 40, target_deploy_id="dep-1", render_deploy_id="dep-3",
                                           render_status="live", started_at=now - timedelta(minutes=3), finished_at=now,
                                           verify=None, error=None),
            }
        office.store.update_candidate(conn, release.id, blocked_reason="Проверка после деплоя не пройдена.", **fields)
        office.store.record_event(conn, task_id, TaskEventType.deploy_status,
                                  {"release_id": release.id, "render_deploy_id": "dep-2", "render_status": "live"})
    return office.store.get_candidate(release.id)


def test_materials_collect_render_logs_and_hide_secrets(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    office.set_task(task_id, rework_reason="Проверка не пройдена, ответ от 10.9.8.7")
    release = failed_release(office, task_id)
    seen = []
    workspace = SimpleNamespace(source_diff=lambda sha, limit: (_ for _ in ()).throw(
        WorkspaceError("fatal: https://x-access-token:ghs_abcdefgh12345678@github.com недоступен")))
    materials = ReworkMaterials(office.settings, office.store, workspace)
    materials.render = render_client("rnd_render_value", "srv-test", transport=httpx.MockTransport(render_handler(seen)))
    task = office.store.get_task(task_id)
    items = materials.collect(task, {"source": "verify_failed", "release_id": release.id, "merge_sha": "c" * 40,
                                     "comment": "Сломан вход, пишите на admin@example.org"})
    bodies = {item["title"]: item["body"] for item in items}
    assert list(bodies) == [
        "Причина отката", "События деплоя", "Логи Render", "Лог упавшей проверки GitHub Actions", "Исходный diff задачи",
    ]
    assert "/healthz не ответил" in bodies["Причина отката"] and "admin@example.org" not in bodies["Причина отката"]
    assert "10.9.8.7" not in bodies["Причина отката"]
    assert "Render dep-3" in bodies["События деплоя"] and "'trigger': 'rollback'" in bodies["События деплоя"]
    logs = bodies["Логи Render"]
    assert "Exception in ASGI application" in logs and "Traceback" in logs
    assert "healthz" not in logs and "сообщение участника" not in logs and "npm run build" not in logs
    assert "10.1.2.3" not in logs and ANT not in logs
    assert "Не применимо" in bodies["Лог упавшей проверки GitHub Actions"]
    assert bodies["Исходный diff задачи"].startswith("логи недоступны")
    assert "ghs_" not in bodies["Исходный diff задачи"] and HIDDEN in bodies["Исходный diff задачи"]
    assert all(len(body.encode("utf-8")) <= SOURCE_LIMIT for body in bodies.values())
    logs_request = [request for request in seen if request.url.path == "/v1/logs"][0]
    params = dict(logs_request.url.params)
    assert (params["ownerId"], params["resource"], params["direction"]) == ("tea-owner", "srv-test", "forward")
    assert params["startTime"] == release.deploy.started_at.isoformat().replace("+00:00", "Z")
    assert params["endTime"] == release.rollback.finished_at.isoformat().replace("+00:00", "Z")


def test_materials_read_next_render_log_pages_and_manual_rollback(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    release = failed_release(office, task_id)
    now = utcnow()
    with office.store.db.locked_transaction() as conn:
        office.store.update_candidate(conn, release.id, status=ReleaseStatus.rolled_back.value, rollback=None)
    rollback = office.add_candidate(
        task_id, kind=ReleaseKind.rollback, commit_sha="a" * 40, expected_previous_sha=release.commit_sha,
        status=ReleaseStatus.released, reason="Кнопка «Выйти» не завершает сеанс.",
    )
    checked = now + timedelta(minutes=10)
    with office.store.db.locked_transaction() as conn:
        office.store.update_candidate(
            conn, rollback.id,
            deploy=DeployResult(render_deploy_id="dep-9", render_status="live", started_at=now, finished_at=now,
                                published_sha=None, error=None),
            verify=VerifyResult(expected_sha="a" * 40, ok=True, fault=False, attempts=1, health=True, version="aaaaaaa",
                                page_version="aaaaaaa", error=None, checked_at=checked),
        )
        office.store.record_event(conn, task_id, TaskEventType.rollback_status,
                                  {"release_id": rollback.id, "render_deploy_id": "dep-9", "render_status": "live"})
    seen = []
    access = {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "info"}],
              "message": '127.0.0.1 - "GET /healthz HTTP/1.1" 200', "timestamp": "t"}

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/services/srv-test":
            return httpx.Response(200, json={"ownerId": "tea-owner"})
        if request.url.path == "/v1/logs" and request.url.params.get("startTime") == "page-2":
            return httpx.Response(200, json={"hasMore": False, "logs": [
                {"labels": [{"name": "type", "value": "app"}, {"name": "level", "value": "error"}],
                 "message": "RuntimeError: logout failed", "timestamp": "t2"},
            ]})
        if request.url.path == "/v1/logs":
            return httpx.Response(200, json={"hasMore": True, "nextStartTime": "page-2", "nextEndTime": "page-2-end",
                                             "logs": [access] * 100})
        deploy_id = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"id": deploy_id, "status": "live", "trigger": "rollback" if deploy_id == "dep-9" else "api"})

    materials = ReworkMaterials(office.settings, office.store, SimpleNamespace(source_diff=lambda sha, limit: "diff"))
    materials.render = render_client("rnd_render_value", "srv-test", transport=httpx.MockTransport(handler))
    items = materials.collect(office.store.get_task(task_id), {"source": "rolled_back", "release_id": release.id,
                                                              "merge_sha": "c" * 40})
    bodies = {item["title"]: item["body"] for item in items}
    assert "RuntimeError: logout failed" in bodies["Логи Render"] and "healthz" not in bodies["Логи Render"]
    pages = [dict(request.url.params) for request in seen if request.url.path == "/v1/logs"]
    assert [page["startTime"] for page in pages][1:] == ["page-2"] and pages[1]["endTime"] == "page-2-end"
    assert pages[0]["endTime"] == checked.isoformat().replace("+00:00", "Z")
    assert "Render dep-9" in bodies["События деплоя"] and f"'release_id': '{rollback.id}'" in bodies["События деплоя"]
    assert "Ручной откат" in bodies["Причина отката"] and "Кнопка «Выйти»" in bodies["Причина отката"]


def test_materials_take_failed_actions_job_log_without_token_on_redirect(office, app_key):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    release = failed_release(office, task_id, deployed=False, run_ids=["1001"])
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.startswith("/app/installations/"):
            return httpx.Response(201, json={"token": "ghs_installation_token_1", "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/actions/runs/1001/jobs"):
            return httpx.Response(200, json={"jobs": [
                {"id": 7, "name": "chat-web", "conclusion": "failure"}, {"id": 8, "name": "office-paths", "conclusion": "success"},
            ]})
        if path.endswith("/actions/jobs/7/logs"):
            return httpx.Response(302, headers={"Location": "https://storage.example.net/logs/7?sig=secret-signature"})
        if request.url.host == "storage.example.net":
            return httpx.Response(200, text="npm run test\nFAIL src/App.test.tsx\nToken ghs_installation_token_1\n")
        return httpx.Response(404, json={})

    materials = ReworkMaterials(office.settings, office.store, SimpleNamespace(source_diff=lambda sha, limit: "diff"))
    materials.github = github_client("1", "2", app_key.path, "SlavaPush/vibe-studio", transport=httpx.MockTransport(handler))
    items = materials.collect(office.store.get_task(task_id), {"source": "checks_failed", "release_id": release.id,
                                                              "merge_sha": "c" * 40})
    actions = items[3]["body"]
    assert "Job chat-web" in actions and "FAIL src/App.test.tsx" in actions
    assert "ghs_installation_token_1" not in actions
    storage = [request for request in seen if request.url.host == "storage.example.net"]
    assert len(storage) == 1 and "Authorization" not in storage[0].headers
    assert "Деплоя не было" in items[1]["body"] and "Деплоя не было" in items[2]["body"]


@pytest.mark.pg
def test_attempt_counts_retries_and_rework_without_offset(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    store = office.store
    assert store.attempt(task_id) == 0
    with store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO decisions (id, participant_id, idempotency_key, operation, object_id, request_hash, decision, "
            "expected_version, recorded_at) VALUES ('dec_1', :owner, 'retry-key-1', 'task_decision', :id, 'h', "
            "'retry', 1, :now)",
            {"owner": me["id"], "id": task_id, "now": to_text(utcnow())},
        )
    assert store.attempt(task_id) == 1
    office.set_task(task_id, rework_count=1)
    assert store.attempt(task_id) == 2


@pytest.mark.pg
def test_rework_adds_limit_of_task_class(office):
    _, me = office.login()
    outcome = IntakeOutcome(True, None, change_class=ChangeClass.backend_api)
    task_id = office.store.create_task(me["id"], "Поле в ответе API.", ModelProfile.hybrid, outcome).id
    office.set_task(task_id, status="failed", failure_kind="verify_failed")
    context = {"source": "verify_failed", "release_id": "rel_x", "owner_id": "rel_x", "merge_sha": "c" * 40}
    with office.store.db.locked_transaction() as conn:
        assert office.store.start_rework_in(conn, task_id, "rel_x", context)
    assert office.store.get_task(task_id).usage.model_calls_limit == 120


@pytest.mark.pg
def test_integration_of_rework_counts_only_own_operations(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    log = OperationLog(office.store.db)
    for key in (f"push:{task_id}:{'a' * 40}", f"merge_pr:{task_id}:{'a' * 40}", f"rework_prepare:{task_id}:1"):
        log.begin(key.split(":")[0], key, task_id)
    office.set_task(task_id, status="needs_attention")
    assert office.store.get_task(task_id).integration_started is True
    office.set_task(task_id, rework_count=1)
    assert office.store.get_task(task_id).integration_started is False
    log.begin("create_pr", f"create_pr:{task_id}-r1", task_id)
    assert office.store.get_task(task_id).integration_started is True


@pytest.mark.pg
def test_attempt_time_starts_at_developing_after_rework(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    store = office.store
    old = utcnow() - timedelta(hours=3)
    with store.db.locked_transaction() as conn:
        store.record_event(conn, task_id, TaskEventType.status_changed, {"from": "awaiting_spec_approval", "to": "developing"})
        store.record_event(conn, task_id, TaskEventType.rework_started, {"attempt": 1})
        conn.execute("UPDATE task_events SET occurred_at = :old WHERE task_id = :id", {"old": to_text(old), "id": task_id})
    assert store.attempt_started_at(task_id) is None
    with store.db.locked_transaction() as conn:
        store.record_event(conn, task_id, TaskEventType.status_changed, {"from": "queued", "to": "developing"})
    started = store.attempt_started_at(task_id)
    assert started is not None and utcnow() - started < timedelta(minutes=1)


def rework_origin(origin):
    source = origin.push(APP, CHANGED)
    reverted = origin.push(APP, ORIGINAL)
    return source, reverted


def rework_task(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    store = app.state.store
    task_id = store.create_task(me["id"], UI_TASK, ModelProfile.hybrid, IntakeOutcome(True, None)).id
    office.set_task(task_id, status="developing")
    app.state.workspace.ensure_worktree(task_id)
    first = app.state.workspace.worktree_path(task_id)
    assert first.name == task_id and first.is_dir()
    source, reverted = rework_origin(origin)
    context = {"source": "verify_failed", "release_id": "rel_x", "owner_id": "rel_x", "merge_sha": source}
    office.set_task(task_id, rework_count=1, base_sha=None, branch=None, rework_context=json.dumps(context))
    return app, task_id, source, reverted, first


def rework_commits(app, task_id, base):
    path = app.state.workspace.worktree_path(task_id)
    log = git(path, "log", "--format=%H %(trailers:key=Office-Rework,valueonly,separator=)", f"{base}..HEAD")
    return [line.split() for line in log.splitlines()]


def prepared(app, task_id):
    return OperationLog(app.state.store.db).find(rework_key(app.state.store.get_task(task_id)))


def test_rework_branch_cherry_picks_source_on_new_main(office, origin, tmp_path):
    app, task_id, source, reverted, first = rework_task(office, origin, tmp_path)
    workspace = app.state.workspace
    workspace.ensure_worktree(task_id)
    task = app.state.store.get_task(task_id)
    path = workspace.worktree_path(task_id)
    assert path.name == f"{task_id}-r1" and not first.exists()
    assert (task.branch, task.base_sha) == (f"office/{task_id}-r1", reverted)
    [[sha, marker]] = rework_commits(app, task_id, reverted)
    assert marker == f"{task_id}-r1"
    assert git(path, "log", "-1", "--format=%(trailers:key=Office-Source,valueonly)", sha) == source
    assert (path / APP).read_text() == CHANGED
    operation = prepared(app, task_id)
    assert (operation.status, operation.attempt, operation.external_ref) == ("succeeded", 1, sha)
    assert workspace.rework_applied(task) is True
    workspace.ensure_worktree(task_id)
    assert len(rework_commits(app, task_id, reverted)) == 1


def crash_on(monkeypatch, workspace, prefix):
    real = workspace.git
    crashed = []

    def git_call(*args, **kwargs):
        if args[:len(prefix)] == prefix and not crashed:
            crashed.append(args)
            raise Crash()
        return real(*args, **kwargs)

    monkeypatch.setattr(workspace, "git", git_call)
    return crashed


@pytest.mark.parametrize("moment", ["before_worktree", "before_cherry_pick", "before_commit", "before_record"])
@pytest.mark.pg
def test_rework_branch_survives_restart_at_each_step(office, origin, tmp_path, monkeypatch, moment):
    app, task_id, source, reverted, _ = rework_task(office, origin, tmp_path)
    workspace = app.state.workspace
    if moment == "before_record":
        real = workspace._rework_done
        monkeypatch.setattr(workspace, "_rework_done", lambda *args: (_ for _ in ()).throw(Crash()))
    else:
        prefix = {"before_worktree": ("worktree", "add"), "before_cherry_pick": ("cherry-pick", "--no-commit"),
                  "before_commit": ("commit",)}[moment]
        crash_on(monkeypatch, workspace, prefix)
    with pytest.raises(Crash):
        workspace.ensure_worktree(task_id)
    assert prepared(app, task_id).status == "started"
    assert json.loads(prepared(app, task_id).detail) == {"base_sha": reverted, "source_sha": source}
    if moment == "before_record":
        monkeypatch.setattr(workspace, "_rework_done", real)
    workspace.ensure_worktree(task_id)
    operation = prepared(app, task_id)
    assert (operation.status, operation.attempt) == ("succeeded", 1)
    commits = rework_commits(app, task_id, reverted)
    assert [marker for _, marker in commits] == [f"{task_id}-r1"]
    assert app.state.store.get_task(task_id).base_sha == reverted
    assert (workspace.worktree_path(task_id) / APP).read_text() == CHANGED


def test_rework_carrier_opens_one_new_pr_beside_first_pass_journal(office, origin, tmp_path, app_key):
    app, task_id, _, reverted, _ = rework_task(office, origin, tmp_path)
    workspace = app.state.workspace
    workspace.ensure_worktree(task_id)
    assert APP.encode() in workspace.diff(app.state.store.get_task(task_id))
    log = OperationLog(app.state.store.db)
    for key in (f"create_pr:{task_id}", f"push:{task_id}:{'a' * 40}"):
        operation, _ = log.begin(key.split(":")[0], key, task_id)
        log.mark(operation, OpStatus.succeeded)
    office.set_task(task_id, status="awaiting_integration")
    fake = FakeGitHub(origin.bare, "ghs_test_token_value")
    fake.prs[1] = {
        "number": 1, "state": "closed", "merged": True, "merge_commit_sha": "c" * 40, "title": "first",
        "head": {"ref": f"office/{task_id}", "sha": "a" * 40}, "html_url": "https://github.com/x/pull/1",
    }
    github = github_client("1", "2", app_key.path, "SlavaPush/vibe-studio", transport=httpx.MockTransport(fake.handler))
    settings = replace(app.state.settings, github_push_url=origin.url)
    store = app.state.store

    def worker():
        return ReleaseWorker(settings, store, workspace, github, REQUIRED, gateway=ScriptedGateway(settings),
                             suite=None, service=httpx.Client(transport=httpx.MockTransport(lambda request: None)))

    for current in (worker(), worker()):
        for _ in range(6):
            if not current.step(task_id):
                break
        current.shutdown()
    task = store.get_task(task_id)
    assert (task.pr_number, task.head_sha) == (2, git(workspace.worktree_path(task_id), "rev-parse", "HEAD"))
    assert fake.prs[2]["head"]["ref"] == f"office/{task_id}-r1"
    assert fake.mutations().count(("POST", "/repos/SlavaPush/vibe-studio/pulls")) == 1
    assert git(origin.bare, "rev-parse", f"office/{task_id}-r1") == task.head_sha
    assert log.find(f"create_pr:{task_id}-r1").status == "succeeded"
    assert log.find(f"push:{task_id}-r1:{task.head_sha}").status == "succeeded"
    assert task.base_sha == reverted


def test_rework_conflict_starts_from_clean_main(office, origin, tmp_path):
    app, task_id, _, _, _ = rework_task(office, origin, tmp_path)
    moved = origin.push(APP, "export function App() {\n  return 'other'\n}\n")
    app.state.workspace.ensure_worktree(task_id)
    task = app.state.store.get_task(task_id)
    path = app.state.workspace.worktree_path(task_id)
    assert task.base_sha == moved == git(path, "rev-parse", "HEAD")
    assert git(path, "status", "--porcelain") == ""
    assert app.state.workspace.rework_applied(task) is False


def test_foreign_head_in_rework_tree_needs_attention(office, origin, tmp_path, monkeypatch):
    app, task_id, _, reverted, _ = rework_task(office, origin, tmp_path)
    workspace = app.state.workspace
    monkeypatch.setattr(workspace, "_rework_done", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        workspace.ensure_worktree(task_id)
    monkeypatch.undo()
    path = workspace.worktree_path(task_id)
    git(path, "commit", "-q", "--amend", "-m", "foreign")
    workspace.ensure_worktree(task_id)
    task = app.state.store.get_task(task_id)
    assert task.status == TaskStatus.needs_attention
    assert prepared(app, task_id).status == "failed"
    assert task.base_sha is None
    assert git(path, "rev-parse", "HEAD^") == reverted


def squash_and_revert(origin, clone, head, title):
    git(clone, "push", "-q", origin.url, f"{head}:refs/heads/office/merged")
    parent = git(origin.bare, "rev-parse", "main")
    tree = git(origin.bare, "rev-parse", f"{head}^{{tree}}")
    merged = git(origin.bare, "commit-tree", tree, "-p", parent, "-m", f"{title} (#1)")
    reverted = git(origin.bare, "commit-tree", f"{parent}^{{tree}}", "-p", merged, "-m", f"Revert {title}")
    git(origin.bare, "update-ref", "refs/heads/main", reverted)
    git(origin.work, "fetch", "-q", str(origin.bare), "main")
    git(origin.work, "reset", "-q", "--hard", "FETCH_HEAD")
    return parent, merged, reverted


@pytest.mark.parametrize("case", ["applied", "conflict", "crash"])
def test_rework_runs_developer_qa_and_critic_with_materials(office, origin, tmp_path, case):
    app = office.graph_app(origin, tmp_path)
    client, me = office.login(app=app)
    store = app.state.store
    task_id = create(app, me["id"])
    approve(client, app, task_id)
    wait_status(app, task_id, TaskStatus.awaiting_integration)
    first = app.state.workspace.worktree_path(task_id)
    task = store.get_task(task_id)
    parent, merged, reverted = squash_and_revert(origin, app.state.workspace.clone, git(first, "rev-parse", "HEAD"), task.title)
    base = reverted
    if case == "conflict":
        base = origin.push(APP, "export function App() {\n  return 'owner'\n}\n")
    office.set_task(task_id, status="failed", failure_kind="verify_failed", merge_sha=merged,
                    rework_reason=f"Проверка после деплоя не пройдена: ответ {FAKE_KEY} от 10.1.2.3")
    release = office.add_candidate(task_id, commit_sha=merged, expected_previous_sha=parent, status=ReleaseStatus.failed)
    if case == "crash":
        collect = app.state.runner.materials.collect

        def crashing(*args):
            with app.state.runner.lock:
                app.state.runner.stopping = True
            raise Crash()

        app.state.runner.materials.collect = crashing
    context = {"source": "verify_failed", "release_id": release.id, "owner_id": release.id, "merge_sha": merged,
               "comment": None, "materials": None}
    with store.db.locked_transaction() as conn:
        assert store.start_rework_in(conn, task_id, release.id, context)
        assert not store.start_rework_in(conn, task_id, release.id, context)
        store._promote(conn)
    if case == "crash":
        wait_for(lambda: not app.state.runner.active(task_id), 10, "crashed run stopped")
        office.stop_app(app)
        app.state.runner.materials.collect = collect
        restarted = ScriptedGateway(office.settings)
        restarted.versions = 100
        app = office.graph_app(origin, tmp_path, gateway=restarted)
        store = app.state.store
    wait_status(app, task_id, TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)
    task = store.get_task(task_id)
    assert task.status == TaskStatus.awaiting_integration, events(app, task_id)[-3:]
    assert transitions(app, task_id)[-4:] == [
        ("failed", "queued"), ("queued", "developing"), ("developing", "reviewing"), ("reviewing", "awaiting_integration"),
    ]
    assert len(events(app, task_id, "rework_started")) == 1
    assert (task.rework_count, task.branch, task.base_sha) == (1, f"office/{task_id}-r1", base)
    assert task.usage.model_calls_limit == 60
    path = app.state.workspace.worktree_path(task_id)
    assert path.name == f"{task_id}-r1" and not first.exists()
    gateway = app.state.gateway
    assert len(gateway.role_calls("supervisor", task_id)) == (0 if case == "crash" else 1)
    prompt = gateway.role_calls("developer", task_id)[-1].user
    assert "Доработка после отката" in prompt and "попытка 1 из 1" in prompt
    assert FAKE_KEY not in prompt and "10.1.2.3" not in prompt and HIDDEN in prompt
    assert "Доработка: Исходный diff задачи" in prompt and APP in prompt
    assert ("не применилось" in prompt) == (case == "conflict")
    for role in ("qa", "critic"):
        assert "Причина доработки после отката" in gateway.role_calls(role, task_id)[-1].user
    log = git(path, "log", "--format=%(trailers:key=Office-Rework,valueonly,separator=)|%(trailers:key=Office-Round,valueonly,separator=)",
              f"{base}..HEAD").splitlines()
    rework = [line for line in log if line.startswith(f"{task_id}-r1|")]
    assert len(rework) == (0 if case == "conflict" else 1)
    assert "|1-1" in log
    saved = store.rework_context(task_id)["materials"]
    assert saved["attempt"] == 1 and len(saved["items"]) == 5
    assert (app.state.settings.artifacts_dir / task_id / "qa-state-1-1.json").is_file()


@pytest.mark.pg
def test_rework_waiting_for_slot_gets_full_time_budget(office):
    _, me = office.login()
    store = office.store
    waiting = [office.queued_task(me["id"]) for _ in range(4)]
    task_id = waiting[-1]
    old = to_text(utcnow() - timedelta(hours=2))
    with store.db.locked_transaction() as conn:
        store.record_event(conn, task_id, TaskEventType.status_changed, {"from": "awaiting_spec_approval", "to": "developing"})
        store.record_event(conn, task_id, TaskEventType.rework_started, {"attempt": 1})
        conn.execute("UPDATE task_events SET occurred_at = :old WHERE task_id = :id", {"old": old, "id": task_id})
        conn.execute("UPDATE tasks SET rework_count = 1, rework_priority = :yes, pr_number = 4, merge_sha = :sha "
                     "WHERE id = :id", {"yes": True, "sha": "c" * 40, "id": task_id})
        promoted = store._promote(conn)
    assert promoted[0] == task_id and len(promoted) == 3
    task = store.get_task(task_id)
    assert (task.status, task.pr_number, task.merge_sha, task.base_sha) == (TaskStatus.developing, None, None, None)
    started = store.attempt_started_at(task_id)
    assert started is not None and utcnow() - started < timedelta(minutes=1)
    assert [store.get_task(item).status for item in waiting[:3]] == [
        TaskStatus.awaiting_spec_approval, TaskStatus.awaiting_spec_approval, TaskStatus.queued,
    ]
