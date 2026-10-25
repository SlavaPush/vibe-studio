import os
import stat

import pytest

from app.schemas import AgentRole
from app.tools import WorkspaceTools


@pytest.fixture
def task(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login()
    [task_id] = office.start_tasks(app, me["id"])
    return app, task_id, app.state.workspace.worktree_path(task_id)


def tools(task, role=AgentRole.developer):
    app, task_id, _ = task
    return WorkspaceTools(app.state.store, app.state.workspace, task_id, role)


def last_event(task):
    app, task_id, _ = task
    return app.state.store.events_after(task_id, 0, 1000)[-1]


def snapshot(root):
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files + dirs:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            content = open(path, "rb").read() if stat.S_ISREG(info.st_mode) else None
            result[os.path.relpath(path, root)] = (info.st_mode, content)
    return result


def test_allowed_write_creates_file(task):
    _, _, root = task
    outcome = tools(task).write_file("apps/chat/web/src/components/chat/Copy.tsx", "export const copy = 1\n")
    assert outcome.result == "ok"
    target = root / "apps/chat/web/src/components/chat/Copy.tsx"
    assert target.read_text() == "export const copy = 1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    event = last_event(task)
    assert event.type == "tool_called"
    assert event.role == AgentRole.developer
    assert event.tool == "write_file"
    assert event.payload == {"arguments": "apps/chat/web/src/components/chat/Copy.tsx (22 байт)", "result": "ok"}
    assert [name for name in os.listdir(target.parent) if name.startswith(".office-")] == []


@pytest.mark.parametrize(
    "path",
    [
        "api/main.py",
        "API/main.py",
        ".git/config",
        ".git",
        ".github/workflows/ci.yml",
        ".env",
        "apps/chat/web/.env.local",
        "apps/chat/web/src/.env",
        "apps/chat/web/package.json",
        "apps/chat/web/package-lock.json",
        "apps/chat/web/vite.config.ts",
        "apps/chat/web/tsconfig.app.json",
        "apps/chat/web/src/App.test.tsx",
        "apps/chat/web/src/test/setup.ts",
        "apps/chat/web/src/new.spec.ts",
        "contracts/api.md",
        "apps/chat/api/main.py",
        "apps/chat/contracts/api.md",
        "apps/chat/web/src/api/client.ts",
        "apps/chat/web/src/mocks/data.ts",
        "apps/office/policies/paths.yaml",
        "README.md",
        "../outside.ts",
        "apps/chat/web/src/../../api/x.ts",
        "/tmp/office-absolute.ts",
        "apps/chat/web/src/script.sh",
        "apps/chat/web/src/App.TEST.tsx",
        "apps/chat/web/e2e/office/a.pw.ts",
    ],
)
def test_forbidden_write_is_denied(task, path):
    _, _, root = task
    before = snapshot(root)
    outcome = tools(task).write_file(path, "export {}\n")
    assert outcome.result == "denied"
    assert outcome.output
    assert snapshot(root) == before
    assert not os.path.exists("/tmp/office-absolute.ts")
    event = last_event(task)
    assert event.tool == "write_file"
    assert event.payload["result"] == "denied"
    assert event.payload["reason"] == outcome.output


def test_oversized_write_is_denied(task):
    _, _, root = task
    outcome = tools(task).write_file("apps/chat/web/src/Big.ts", "x" * 204801)
    assert outcome.result == "denied"
    assert not (root / "apps/chat/web/src/Big.ts").exists()


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_symlink_is_denied(task, tmp_path, kind):
    _, _, root = task
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.ts").write_text("original\n")
    if kind == "directory":
        os.symlink(outside, root / "apps/chat/web/src/linked")
        path = "apps/chat/web/src/linked/target.ts"
    else:
        os.symlink(outside / "target.ts", root / "apps/chat/web/src/target.ts")
        path = "apps/chat/web/src/target.ts"
    outcome = tools(task).write_file(path, "changed\n")
    assert outcome.result == "denied"
    assert (outside / "target.ts").read_text() == "original\n"
    assert tools(task).read_file(path).result == "denied"


def test_qa_writes_only_own_directory(task):
    _, _, root = task
    qa = tools(task, AgentRole.qa)
    assert qa.write_file("apps/chat/web/e2e/office/copy.pw.ts", "import { test } from '@playwright/test'\n").result == "ok"
    assert (root / "apps/chat/web/e2e/office/copy.pw.ts").is_file()
    for path in ["apps/chat/web/src/App.tsx", "apps/chat/web/e2e/office/helper.ts", "apps/chat/web/e2e/other.pw.ts"]:
        assert qa.write_file(path, "x\n").result == "denied"
    assert (root / "apps/chat/web/src/App.tsx").read_text().startswith("export function App")


def test_read_and_list(task):
    _, _, root = task
    developer = tools(task)
    assert developer.read_file("apps/chat/web/src/App.tsx").output.startswith("export function App")
    assert developer.read_file(".git").result == "denied"
    (root / ".env").write_text("SECRET=1\n")
    assert developer.read_file(".env").result == "denied"
    listing = developer.list_dir(".").output.splitlines()
    assert listing == ["README.md", "apps/"]
    assert developer.list_dir("apps").output.splitlines() == ["chat/"]
    assert developer.list_dir("apps/chat/web/src").output.splitlines() == ["App.test.tsx", "App.tsx", "test/"]
    assert developer.read_file("apps/office/policies/paths.yaml").result == "error"
    assert developer.read_file("apps/chat/web/src/Missing.tsx").result == "error"


def test_delete_file(task):
    _, _, root = task
    developer = tools(task)
    assert developer.write_file("apps/chat/web/src/Old.tsx", "x\n").result == "ok"
    assert developer.delete_file("apps/chat/web/src/Old.tsx").result == "ok"
    assert not (root / "apps/chat/web/src/Old.tsx").exists()
    assert developer.delete_file("apps/chat/web/src/App.test.tsx").result == "denied"
    assert (root / "apps/chat/web/src/App.test.tsx").is_file()


def test_backend_developer_changes_chat_tests(task, office):
    _, task_id, root = task
    frontend = tools(task)
    office.set_task(task_id, change_class="backend_api")
    developer = tools(task)
    qa = tools(task, AgentRole.qa)
    for path in (
        "apps/chat/api/tests/test_models.py",
        "apps/chat/web/src/components/chat/ModelSelector.test.tsx",
        "apps/chat/web/src/test/fixtures.ts",
        "apps/chat/web/e2e/office/support/mock-api.ts",
    ):
        assert frontend.write_file(path, "x\n").result == "denied"
        assert qa.write_file(path, "x\n").result == "denied"
        assert developer.write_file(path, "x\n").result == "ok"
        assert (root / path).read_text() == "x\n"
    for path in ("apps/chat/api/tests/office/test_tsk_1.py", "apps/chat/web/e2e/office/a.pw.ts"):
        assert developer.write_file(path, "x\n").result == "denied"
    assert frontend.delete_file("apps/chat/web/src/App.test.tsx").result == "denied"
    assert developer.delete_file("apps/chat/web/src/App.test.tsx").result == "ok"
    assert not (root / "apps/chat/web/src/App.test.tsx").exists()
