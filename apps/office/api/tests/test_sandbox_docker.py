import json
import os
import re
import subprocess
import uuid
from dataclasses import replace

import pytest
import yaml
from conftest import Office, git

from app.python_sandbox import PythonSandbox
from app.sandbox import Sandbox
from app.schemas import AgentRole, ChangeClass, TaskStatus
from app.tools import WorkspaceTools

TARGET = os.environ.get("OFFICE_TEST_TARGET_REPO", "https://github.com/SlavaPush/vibe-studio.git")
FEATURE_TEST = """import { test, expect } from "@playwright/test";

test("заголовок новой версии", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page).toHaveTitle("vibe-chat office");
  await page.screenshot({ path: testInfo.outputPath("home.png") });
});
"""
MOCK_TEST = """import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("заголовок новой версии и ответ ассистента", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await expect(page).toHaveTitle("vibe-chat office", { timeout: 5000 });
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();
  await page.getByLabel("Сообщение", { exact: true }).fill("Привет");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(page.getByText(fixtures.reply, { exact: true })).toBeVisible();
});
"""
PASSED = re.compile(r"^\s+\d+ passed", re.MULTILINE)
FAILED = re.compile(r"^\s+\d+ failed", re.MULTILINE)
PROBE ="node -e \"fetch('https://registry.npmjs.org').then(() => process.exit(0), () => process.exit(3))\""

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not os.environ.get("OFFICE_TEST_DOCKER"), reason="OFFICE_TEST_DOCKER не задан"),
]


@pytest.fixture
def real(tmp_path):
    office = Office(f"sqlite:///{tmp_path / 'office.sqlite'}", tmp_path)
    app = office.new_app(workspace=True, target_repo_url=TARGET, workspace_dir=tmp_path / "workspace")
    prefix = f"office-nm-test-{uuid.uuid4().hex[:8]}-"
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, volume_prefix=prefix)
    client, me = office.login(app=app)
    yield office, app, sandbox, client, me, prefix
    office.close()
    listed = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"], capture_output=True, text=True, check=True
    )
    for volume in listed.stdout.split():
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)


def logs(client, task_id, report):
    return "\n".join(
        client.get(url.removeprefix("https://testserver")).text[-3000:]
        for check in report.checks
        for url in check.artifact_urls[:1]
    )


def test_real_sandbox_on_vibe_chat(real, tmp_path):
    office, app, sandbox, client, me, prefix = real
    [task_id] = office.start_tasks(app, me["id"])
    store = app.state.store
    workspace = app.state.workspace
    task = store.get_task(task_id)
    assert task.status == TaskStatus.awaiting_spec_approval, store.events_after(task_id, 0, 100)
    root = workspace.worktree_path(task_id)
    base = task.base_sha

    report = sandbox.run_suite(task_id, base, base)
    assert [check.command.split()[:3] for check in report.checks] == [
        ["npm", "ci", "--no-audit"], ["npm", "run", "lint"], ["npm", "run", "test"], ["npm", "run", "build"]
    ]
    assert [check.exit_code for check in report.checks] == [0, 0, 0, 0], logs(client, task_id, report)
    assert report.commit_sha == git(root, "rev-parse", "HEAD")
    volume = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(volume) == 1
    owner = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,source={volume[0]},target=/deps,readonly",
         sandbox.image, "stat", "-c", "%u", "/deps/node_modules"],
        capture_output=True, text=True, check=True,
    )
    assert owner.stdout.strip() == "10001"

    developer = WorkspaceTools(store, workspace, task_id, AgentRole.developer)
    page = (root / "apps/chat/web/index.html").read_text()
    page = page.replace("<title>vibe-chat</title>", "<title>vibe-chat office</title>")
    assert developer.write_file("apps/chat/web/index.html", page).result == "ok"
    qa = WorkspaceTools(store, workspace, task_id, AgentRole.qa)
    assert qa.write_file("apps/chat/web/e2e/office/title.pw.ts", FEATURE_TEST).result == "ok"
    commit = workspace.commit(task_id, "Заголовок офиса")

    before = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    after = sandbox.run_suite(task_id, commit, commit, require_e2e=True)
    assert [check.exit_code for check in before.checks][:3] == [0, 0, 0], logs(client, task_id, before)
    assert before.checks[-1].exit_code != 0, logs(client, task_id, before)
    assert [check.exit_code for check in after.checks] == [0, 0, 0, 0], logs(client, task_id, after)
    assert before.commit_sha == base
    assert after.commit_sha == commit == git(root, "rev-parse", "HEAD")
    assert before.suite_hash == after.suite_hash
    assert client.get(f"/api/tasks/{task_id}/checks").json() == after.model_dump(mode="json")

    urls = after.checks[-1].artifact_urls
    log = client.get(urls[0])
    assert log.status_code == 200
    assert "title.pw.ts" in log.text and PASSED.search(log.text) and not FAILED.search(log.text)
    [shot] = [url for url in urls if url.endswith("home.png")]
    image = client.get(shot)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    [unmocked] = [url for url in urls if url.endswith("unmocked-api.txt")]
    assert "GET /api/" in client.get(unmocked).text
    outcome = sandbox.e2e_result(task_id, after, "apps/chat/web/e2e/office/title.pw.ts")
    assert outcome.status == "invalid" and "page.route" in outcome.reason

    policy = yaml.safe_load(app.state.settings.commands_policy.read_text(encoding="utf-8"))
    policy["steps"]["probe"] = {"command": PROBE, "network": False, "timeout_seconds": 120, "roles": ["developer"]}
    policy_path = tmp_path / "commands.yaml"
    policy_path.write_text(yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
    probe = Sandbox(replace(app.state.settings, commands_policy=policy_path), store, workspace, volume_prefix=prefix)
    result = probe.run_step(task_id, "probe", AgentRole.developer)
    assert result.exit_code == 3, client.get(result.artifact_urls[0]).text


def test_mock_api_module_in_real_sandbox(real):
    office, app, sandbox, client, me, prefix = real
    [task_id] = office.start_tasks(app, me["id"])
    store = app.state.store
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    base = store.get_task(task_id).base_sha
    assert (root / "apps/chat/web/e2e/office/support/mock-api.ts").is_file()

    developer = WorkspaceTools(store, workspace, task_id, AgentRole.developer)
    page = (root / "apps/chat/web/index.html").read_text()
    assert developer.write_file(
        "apps/chat/web/index.html", page.replace("<title>vibe-chat</title>", "<title>vibe-chat office</title>")
    ).result == "ok"
    qa = WorkspaceTools(store, workspace, task_id, AgentRole.qa)
    test_file = f"apps/chat/web/e2e/office/{task_id}.pw.ts"
    assert qa.write_file(test_file, MOCK_TEST).result == "ok"
    assert qa.write_file("apps/chat/web/e2e/office/support/mock-api.ts", "export {}\n").result == "denied"
    commit = workspace.commit(task_id, "Заголовок офиса")

    before = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    after = sandbox.run_suite(task_id, commit, commit, require_e2e=True)
    assert [check.exit_code for check in before.checks][:3] == [0, 0, 0], logs(client, task_id, before)
    assert [check.exit_code for check in after.checks] == [0, 0, 0, 0], logs(client, task_id, after)
    failed = sandbox.e2e_result(task_id, before, test_file)
    assert failed.status == "failed" and "toHaveTitle" in failed.message, logs(client, task_id, before)
    assert failed.line == 7
    assert sandbox.e2e_result(task_id, after, test_file).status == "passed"
    for report in (before, after):
        assert not [url for url in report.checks[-1].artifact_urls if url.endswith("unmocked-api.txt")]
    log = client.get(after.checks[-1].artifact_urls[0]).text
    assert f"{task_id}.pw.ts" in log and PASSED.search(log) and not FAILED.search(log)


QA_PYTEST = """import importlib.util


def test_probe_module_exists():
    assert importlib.util.find_spec("app.office_probe") is not None
"""


@pytest.mark.skipif(not os.environ.get("OFFICE_TEST_PYTHON_SANDBOX"), reason="OFFICE_TEST_PYTHON_SANDBOX не задан")
def test_real_pytest_qa_fails_on_base_and_passes_on_task(real):
    office, app, sandbox, client, me, prefix = real
    sandbox.python = PythonSandbox(sandbox, volume_prefix=f"office-py-test-{uuid.uuid4().hex[:8]}-")
    [task_id] = office.start_tasks(app, me["id"])
    office.set_task(task_id, change_class="backend_api")
    store = app.state.store
    workspace = app.state.workspace
    base = store.get_task(task_id).base_sha
    developer = WorkspaceTools(store, workspace, task_id, AgentRole.developer)
    assert developer.write_file("apps/chat/api/app/office_probe.py", "VALUE = 1\n").result == "ok"
    qa = WorkspaceTools(store, workspace, task_id, AgentRole.qa)
    test_file = f"apps/chat/api/tests/office/test_{task_id}.py"
    assert qa.write_file(test_file, QA_PYTEST).result == "ok"
    commit = workspace.commit(task_id, "Модуль проверки")
    try:
        before = sandbox.run_suite(task_id, base, commit, require_e2e=True, change_class=ChangeClass.backend_api)
        after = sandbox.run_suite(task_id, commit, commit, require_e2e=True, change_class=ChangeClass.backend_api)
    finally:
        sandbox.python.remove_stale()
        listed = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={sandbox.python.volume_prefix}"],
            capture_output=True, text=True, check=True,
        )
        for volume in listed.stdout.split():
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)
    failed = sandbox.e2e_result(task_id, before, test_file)
    assert failed.status == "failed" and "AssertionError" in failed.message, logs(client, task_id, before)
    assert failed.line == 5
    passed = sandbox.e2e_result(task_id, after, test_file)
    assert passed.status == "passed", logs(client, task_id, after)
    assert any(url.endswith("junit.xml") for url in after.checks[-1].artifact_urls)
    assert all(check.exit_code == 0 for check in after.checks), logs(client, task_id, after)


def test_real_lock_step_updates_package_lock(real, tmp_path):
    office, app, sandbox, client, me, prefix = real
    [task_id] = office.start_tasks(app, me["id"])
    root = app.state.workspace.worktree_path(task_id)
    web = root / "apps/chat/web"
    manifest = json.loads((web / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"]["left-pad"] = "1.3.0"
    (web / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    before = (web / "package-lock.json").read_text(encoding="utf-8")
    result = sandbox.lock_node(task_id, root)
    assert result.exit_code == 0, client.get(result.artifact_urls[0]).text
    after = json.loads((web / "package-lock.json").read_text(encoding="utf-8"))
    assert after != json.loads(before)
    assert after["packages"][""]["dependencies"]["left-pad"] == "1.3.0"
    assert after["packages"]["node_modules/left-pad"]["version"] == "1.3.0"
    assert not (web / "node_modules" / "left-pad").exists()
