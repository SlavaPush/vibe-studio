import pytest
from conftest import BASE_URL, git
from fastapi.testclient import TestClient

from app.workspace import DIFF_LIMIT


@pytest.fixture
def started(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    client, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    return app, client, task_id, app.state.workspace.worktree_path(task_id)


def test_diff_includes_changed_and_new_files(started):
    _, client, task_id, root = started
    (root / "web/src/App.tsx").write_text("export function App() {\n  return 'Привет'\n}\n")
    (root / "web/src/New.tsx").write_text("export const created = true\n")
    response = client.get(f"/api/tasks/{task_id}/diff")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    text = response.text
    assert "diff --git a/web/src/App.tsx b/web/src/App.tsx" in text
    assert "+  return 'Привет'" in text
    assert "diff --git a/web/src/New.tsx b/web/src/New.tsx\nnew file mode 100644" in text
    assert "+export const created = true" in text


def test_diff_includes_commits_since_base(started):
    app, client, task_id, root = started
    (root / "web/src/Committed.tsx").write_text("export {}\n")
    app.state.workspace.commit(task_id, "feature")
    assert "b/web/src/Committed.tsx" in client.get(f"/api/tasks/{task_id}/diff").text


def test_diff_keeps_worktree_index(started):
    _, client, task_id, root = started
    (root / "web/src/App.tsx").write_text("staged\n")
    git(root, "add", "web/src/App.tsx")
    (root / "web/src/Untracked.tsx").write_text("new\n")
    index = root / git(root, "rev-parse", "--git-path", "index")
    index_before = index.read_bytes()
    cached_before = git(root, "diff", "--cached")
    status_before = git(root, "status", "--porcelain")
    response = client.get(f"/api/tasks/{task_id}/diff")
    assert response.status_code == 200
    assert "b/web/src/Untracked.tsx" in response.text
    assert index.read_bytes() == index_before
    assert git(root, "diff", "--cached") == cached_before
    assert git(root, "status", "--porcelain") == status_before


def test_large_diff_is_rejected(started):
    _, client, task_id, root = started
    (root / "web/src/Large.tsx").write_text(("x" * 99 + "\n") * (DIFF_LIMIT // 100 + 10))
    response = client.get(f"/api/tasks/{task_id}/diff")
    assert response.status_code == 413
    assert response.json()["code"] == "diff_too_large"


def test_diff_without_worktree_is_not_found(office, started):
    app, client, _, _ = started
    _, me = office.login()
    queued = office.queued_task(me["id"])
    response = client.get(f"/api/tasks/{queued}/diff")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert client.get("/api/tasks/tsk_missing/diff").status_code == 404


def test_diff_without_workspace_is_not_found(office):
    client, me = office.login()
    task_id = office.queued_task(me["id"])
    office.set_task(task_id, base_sha="b" * 40)
    response = client.get(f"/api/tasks/{task_id}/diff")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_diff_requires_session(started):
    app, _, task_id, _ = started
    anonymous = TestClient(app, base_url=BASE_URL)
    assert anonymous.get(f"/api/tasks/{task_id}/diff").status_code == 401
