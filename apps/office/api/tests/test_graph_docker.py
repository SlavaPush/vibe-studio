import json
import os
import subprocess
import uuid

import pytest
from conftest import UI_TASK, Office, ScriptedGateway, developer_answer, wait_for

from app.runner import Checkpoints, Runner
from app.sandbox import Sandbox
from app.schemas import ModelProfile, TaskStatus
from app.tasks import IntakeOutcome

TARGET = os.environ.get("OFFICE_TEST_TARGET_REPO", "https://github.com/SlavaPush/vibe-chat.git")
TITLE_TEST = """import { test, expect } from "@playwright/test";

test("заголовок новой версии", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("vibe-chat office", { timeout: 5000 });
});
"""

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not os.environ.get("OFFICE_TEST_DOCKER"), reason="OFFICE_TEST_DOCKER не задан"),
]


@pytest.fixture
def real(tmp_path):
    office = Office(f"sqlite:///{tmp_path / 'office.sqlite'}", tmp_path)
    app = office.new_app(workspace=True, target_repo_url=TARGET, workspace_dir=tmp_path / "workspace")
    prefix = f"office-nm-test-{uuid.uuid4().hex[:8]}-"
    settings = app.state.settings
    sandbox = Sandbox(settings, app.state.store, app.state.workspace, volume_prefix=prefix)
    gateway = ScriptedGateway(settings)
    app.state.runner = Runner(
        settings, app.state.store, app.state.workspace, sandbox, gateway, Checkpoints(settings.database_url)
    )
    yield office, app, gateway
    office.close()
    listed = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"], capture_output=True, text=True, check=True
    )
    for volume in listed.stdout.split():
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)


def test_graph_with_real_sandbox_produces_qa_evidence(real):
    office, app, gateway = real
    store = app.state.store
    workspace = app.state.workspace
    sandbox = app.state.runner.sandbox
    stale = f"office-stale-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "create", "--label", sandbox.label, "--name", stale, sandbox.image, "true"],
        capture_output=True, check=True,
    )
    app.state.runner.recover()
    found = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={stale}"], capture_output=True, text=True, check=True)
    assert found.stdout.strip() == ""

    def developer(call):
        page = (workspace.worktree_path(call.task_id) / "web/index.html").read_text()
        return developer_answer(page.replace("<title>vibe-chat</title>", "<title>vibe-chat office</title>"), "web/index.html")

    def qa(call):
        path = f"web/e2e/office/{call.task_id}.pw.ts"
        return {
            "actions": [{"kind": "write_file", "path": path, "content": TITLE_TEST, "step": None}],
            "done": True,
            "summary": "Тест заголовка",
            "criteria_covered": [1],
            "findings": [],
        }

    gateway.handlers["developer"] = developer
    gateway.handlers["qa"] = qa
    client, me = office.login(app=app)
    task_id = store.create_task(me["id"], UI_TASK, ModelProfile.hybrid, IntakeOutcome(True, None)).id
    store.promote_queue()
    expected = wait_for(
        lambda: [event for event in store.events_after(task_id, 0, 1000) if event.type == "decision_required"], 120, "spec"
    )[-1].payload["expected_version"]
    response = client.post(
        f"/api/tasks/{task_id}/decisions",
        json={"decision": "approve_spec", "expected_version": expected, "idempotency_key": "approve-docker"},
    )
    assert response.status_code == 200, response.text
    wait_for(
        lambda: store.get_task(task_id).status in (TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention),
        1500,
        "graph",
    )
    task = store.get_task(task_id)
    finished = [event.payload for event in store.events_after(task_id, 0, 10000) if event.type == "role_finished"]
    assert task.status == TaskStatus.awaiting_integration, finished

    with store.db.connection() as conn:
        rows = conn.fetchall("SELECT commit_sha, checks FROM check_reports WHERE task_id = :id ORDER BY created_at", {"id": task_id})
    command = "playwright test --config /office-e2e/playwright.config.ts"
    e2e = [(row["commit_sha"], check["exit_code"]) for row in rows for check in json.loads(row["checks"]) if check["command"] == command]
    head = workspace.head(task_id)
    assert e2e == [(task.base_sha, 1), (head, 0)]
    artifacts = app.state.settings.artifacts_dir / task_id
    reports = sorted(artifacts.glob("e2e-*-playwright-report.json"))
    assert len(reports) == 2
    before = json.loads(reports[0].read_text())
    assert before["errors"] == []
    [spec] = [spec for suite in before["suites"] for child in [suite, *suite.get("suites", [])] for spec in child.get("specs", [])]
    assert spec["file"].endswith(f"{task_id}.pw.ts")
    assert spec["tests"][0]["results"][-1]["status"] == "failed"
    qa_review = json.loads((artifacts / "review-0-qa.json").read_text())
    assert qa_review["qa_sha"] == head and qa_review["findings"] == []
    leftovers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=office-{task_id}"], capture_output=True, text=True, check=True
    )
    assert leftovers.stdout.strip() == ""
