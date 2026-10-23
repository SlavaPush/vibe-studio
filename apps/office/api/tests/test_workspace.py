import os
import shutil

import pytest
from conftest import git

from app.main import create_app
from app.paths import PathDenied
from app.schemas import ModelProfile, TaskStatus
from app.tasks import IntakeOutcome
from app.workspace import Workspace


@pytest.fixture
def owner(office):
    _, me = office.login()
    return me["id"]


@pytest.fixture
def app(office, origin, tmp_path):
    return office.workspace_app(origin, tmp_path)


def tool_events(store, task_id, tool):
    return [event for event in store.events_after(task_id, 0, 1000) if event.tool == tool]


def head_branch(path):
    return git(path, "symbolic-ref", "--short", "HEAD")


def transition(store, task_id, to):
    with store.db.locked_transaction() as conn:
        task = store.load_task(conn, task_id)
        store.transition(conn, task_id, to, expected_from=task.status)


def test_three_tasks_get_three_worktrees(office, app, origin, owner):
    ids = office.start_tasks(app, owner, 3)
    store = app.state.store
    workspace = app.state.workspace
    paths = set()
    for task_id in ids:
        task = store.get_task(task_id)
        path = workspace.worktree_path(task_id)
        assert task.status == TaskStatus.awaiting_spec_approval
        assert task.branch == f"office/{task_id}"
        assert task.base_sha == origin.main()
        assert path == workspace.root / "wt" / task_id
        assert head_branch(path) == f"office/{task_id}"
        assert (path / "web" / "src" / "App.tsx").is_file()
        events = tool_events(store, task_id, "git_worktree")
        assert [event.payload for event in events] == [
            {"arguments": f"office/{task_id} {origin.main()}", "result": "ok"}
        ]
        paths.add(path)
    assert len(paths) == 3


def test_fetch_error_fails_task(office, app, origin, owner):
    office.start_tasks(app, owner)
    origin.bare.rename(origin.bare.with_name("moved.git"))
    [task_id] = office.start_tasks(app, owner)
    task = app.state.store.get_task(task_id)
    assert task.status == TaskStatus.failed
    assert task.branch is None
    [event] = tool_events(app.state.store, task_id, "git_worktree")
    assert event.payload["result"] == "error"
    assert event.payload["reason"].startswith("git fetch")
    assert not app.state.workspace.worktree_path(task_id).exists()


def test_file_error_fails_task(office, app, owner, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.workspace.tempfile.mkdtemp", denied)
    [task_id] = office.start_tasks(app, owner)
    task = app.state.store.get_task(task_id)
    assert task.status == TaskStatus.failed
    [event] = tool_events(app.state.store, task_id, "git_worktree")
    assert event.payload["result"] == "error"
    assert event.payload["reason"] == "Файловая операция рабочей области не выполнена: Permission denied."


def test_new_instance_restores_and_reuses_worktrees(office, app, origin, owner, tmp_path):
    first, second = office.start_tasks(app, owner, 2)
    store = app.state.store
    workspace = app.state.workspace
    (workspace.worktree_path(first) / "web" / "src" / "Feature.tsx").write_text("export {}\n")
    commit = workspace.commit(first, "feature")
    (workspace.worktree_path(second) / "web" / "src" / "Draft.tsx").write_text("draft\n")
    before = {task_id: store.get_task(task_id) for task_id in (first, second)}
    workspace.shutdown()
    shutil.rmtree(workspace.worktree_path(first))

    restarted = office.workspace_app(origin, tmp_path)
    restarted.state.workspace.wait_idle()
    after = {task_id: restarted.state.store.get_task(task_id) for task_id in (first, second)}
    path = workspace.worktree_path(first)
    assert git(path, "rev-parse", "HEAD") == commit
    assert head_branch(path) == f"office/{first}"
    assert after[first].base_sha == before[first].base_sha
    assert after[first].version == before[first].version
    assert len(tool_events(restarted.state.store, first, "git_worktree")) == 2
    assert (workspace.worktree_path(second) / "web" / "src" / "Draft.tsx").read_text() == "draft\n"
    assert after[second].version == before[second].version
    assert len(tool_events(restarted.state.store, second, "git_worktree")) == 1


def test_branch_without_base_sha_uses_merge_base(office, app, origin, owner):
    office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    start = origin.main()
    task_id = store.create_task(owner, "Добавить подсказку к полю ввода сообщения в чате.", ModelProfile.hybrid,
                                IntakeOutcome(True, None)).id
    git(workspace.clone, "branch", f"office/{task_id}", "origin/main")
    origin.push("web/src/Other.tsx", "export {}\n")
    git(workspace.clone, "fetch", "-q", "origin")
    store.promote_queue()
    workspace.wait_idle()
    task = store.get_task(task_id)
    assert task.base_sha == start != origin.main()
    assert task.branch == f"office/{task_id}"
    assert head_branch(workspace.worktree_path(task_id)) == f"office/{task_id}"


def test_foreign_directory_fails_task(office, app, owner):
    office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    task_id = store.create_task(owner, "Добавить подсказку к полю ввода сообщения в чате.", ModelProfile.hybrid,
                                IntakeOutcome(True, None)).id
    foreign = workspace.worktree_path(task_id)
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("keep")
    store.promote_queue()
    workspace.wait_idle()
    assert store.get_task(task_id).status == TaskStatus.failed
    [event] = tool_events(store, task_id, "git_worktree")
    assert event.payload["result"] == "error"
    assert "не является worktree" in event.payload["reason"]
    assert (foreign / "notes.txt").read_text() == "keep"


def test_cancel_during_creation_removes_worktree(office, app, owner, monkeypatch):
    store = app.state.store
    workspace = app.state.workspace
    prepare = Workspace._prepare

    def prepare_then_cancel(self, task):
        result = prepare(self, task)
        transition(store, task.id, TaskStatus.cancelled)
        return result

    monkeypatch.setattr(Workspace, "_prepare", prepare_then_cancel)
    [task_id] = office.start_tasks(app, owner)
    task = store.get_task(task_id)
    assert task.status == TaskStatus.cancelled
    assert task.branch is None
    assert not workspace.worktree_path(task_id).exists()
    assert git(workspace.clone, "branch", "--list", f"office/{task_id}")


@pytest.mark.parametrize("closed", [TaskStatus.cancelled, TaskStatus.released])
def test_closed_task_removes_worktree_and_keeps_branch(office, app, owner, closed):
    [task_id] = office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    if closed == TaskStatus.released:
        office.set_task(task_id, status="deploying")
    transition(store, task_id, closed)
    workspace.wait_idle()
    assert not workspace.worktree_path(task_id).exists()
    assert git(workspace.clone, "branch", "--list", f"office/{task_id}")
    assert f"wt/{task_id}" not in git(workspace.clone, "worktree", "list")


def test_failed_task_keeps_worktree_and_retry_reuses_it(office, app, owner):
    [task_id] = office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    draft = workspace.worktree_path(task_id) / "web" / "src" / "Draft.tsx"
    draft.write_text("draft\n")
    transition(store, task_id, TaskStatus.failed)
    workspace.wait_idle()
    assert draft.is_file()
    transition(store, task_id, TaskStatus.queued)
    store.promote_queue()
    workspace.wait_idle()
    assert store.get_task(task_id).status == TaskStatus.awaiting_spec_approval
    assert draft.read_text() == "draft\n"


def test_startup_removes_worktrees_of_closed_tasks(office, app, origin, owner, tmp_path):
    [task_id] = office.start_tasks(app, owner)
    app.state.workspace.shutdown()
    office.set_task(task_id, status="cancelled")
    restarted = office.workspace_app(origin, tmp_path)
    restarted.state.workspace.wait_idle()
    assert not restarted.state.workspace.worktree_path(task_id).exists()


def test_default_factory_reads_environment(origin, tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_DATABASE_URL", f"sqlite:///{tmp_path / 'env.sqlite'}")
    monkeypatch.setenv("TARGET_REPO_URL", origin.url)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "env-workspace"))
    monkeypatch.setenv("OFFICE_ARTIFACTS_DIR", str(tmp_path / "env-artifacts"))
    apps = []
    try:
        apps.append(create_app())
        store = apps[0].state.store
        with store.db.transaction() as conn:
            conn.execute(
                "INSERT INTO participants (id, display_name, role, created_at) "
                "VALUES ('prt_env', 'Env', 'participant', '2026-10-23T00:00:00.000000Z')"
            )
        task_id = store.create_task("prt_env", "Добавить подсказку к полю ввода сообщения в чате.",
                                    ModelProfile.hybrid, IntakeOutcome(True, None)).id
        store.promote_queue()
        apps[0].state.workspace.wait_idle()
        path = tmp_path / "env-workspace" / "wt" / task_id
        assert head_branch(path) == f"office/{task_id}"
        apps[0].state.workspace.shutdown()
        shutil.rmtree(path)
        apps.append(create_app())
        apps[1].state.workspace.wait_idle()
        assert head_branch(path) == f"office/{task_id}"
        monkeypatch.delenv("TARGET_REPO_URL")
        with pytest.raises(ValueError, match="TARGET_REPO_URL"):
            create_app()
    finally:
        for item in apps:
            item.state.workspace.shutdown()
            item.state.store.db.close()


def write(path, rel, content="export {}\n"):
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_commit_updates_changed_files(office, app, owner):
    [task_id] = office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    path = workspace.worktree_path(task_id)
    write(path, "web/src/App.tsx", "export function App() {\n  return 1\n}\n")
    write(path, "web/src/components/New.tsx")
    write(path, "web/e2e/office/new.pw.ts")
    version = store.get_task(task_id).version
    sha = workspace.commit(task_id, "feature")
    task = store.get_task(task_id)
    assert git(path, "rev-parse", "HEAD") == sha
    assert git(path, "log", "-1", "--format=%an <%ae>") == "back-office <office@localhost>"
    assert task.changed_files == ["web/e2e/office/new.pw.ts", "web/src/App.tsx", "web/src/components/New.tsx"]
    assert task.version == version + 1
    assert git(path, "status", "--porcelain") == ""
    [event] = tool_events(store, task_id, "git_commit")
    assert event.payload == {"arguments": f"{sha} (3 файлов)", "result": "ok"}
    assert workspace.commit(task_id, "nothing") is None


@pytest.mark.parametrize(
    "change",
    ["forbidden_path", "existing_test", "symlink", "executable", "deleted_test", "case_variant"],
)
def test_commit_rejects_policy_violation(office, app, owner, change):
    [task_id] = office.start_tasks(app, owner)
    store = app.state.store
    workspace = app.state.workspace
    path = workspace.worktree_path(task_id)
    write(path, "web/src/Allowed.tsx")
    git(path, "add", "web/src/Allowed.tsx")
    if change == "forbidden_path":
        write(path, "api/main.py", "app = 1\n")
    elif change == "existing_test":
        write(path, "web/src/App.test.tsx", "// weakened\n")
    elif change == "symlink":
        os.symlink("App.tsx", path / "web" / "src" / "Link.tsx")
    elif change == "executable":
        write(path, "web/src/run.ts").chmod(0o755)
    elif change == "deleted_test":
        (path / "web" / "src" / "test" / "setup.ts").unlink()
    else:
        write(path, "API/extra.py", "x = 1\n")
    index_path = path / git(path, "rev-parse", "--git-path", "index")
    index_before = index_path.read_bytes()
    head = git(path, "rev-parse", "HEAD")
    version = store.get_task(task_id).version
    with pytest.raises(PathDenied):
        workspace.commit(task_id, "bad")
    assert git(path, "rev-parse", "HEAD") == head
    assert index_path.read_bytes() == index_before
    assert store.get_task(task_id).version == version
    assert store.get_task(task_id).changed_files == []
    [event] = tool_events(store, task_id, "git_commit")
    assert event.payload["result"] == "denied"
