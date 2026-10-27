import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import json

import pytest
from conftest import git, wait_for

from app.sandbox import (
    MARKER,
    UNAVAILABLE,
    CappedLog,
    Sandbox,
    SandboxCancelled,
    SandboxError,
    StepDenied,
    failed_tests,
    parse_playwright,
    run_process,
    unmocked_reason,
    unmocked_requests,
)
from app.schemas import AgentRole, CheckReport, CheckResult


def git_head(path):
    return git(path, "rev-parse", "HEAD")


def mounts(args):
    result = {}
    for index, value in enumerate(args):
        if value == "--mount":
            fields = dict(item.split("=", 1) for item in args[index + 1].split(",") if "=" in item)
            result[fields["target"]] = (fields["type"], fields["source"], args[index + 1].endswith(",readonly"))
    return result


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.volumes = {}
        self.image = True
        self.image_id = None
        self.delay = 0.0
        self.install_exit = 0
        self.exit_codes = {}
        self.timeouts = set()
        self.running = 0
        self.max_running = 0
        self.qa_files = {}
        self.marker = True
        self.output = b""
        self.hooks = {}
        self.created = {}
        self.on_create = None
        self.lock = threading.Lock()

    def runs(self, step=None):
        found = [args for args in self.calls if args[:2] == ["docker", "create"] and "sandbox-step" in args]
        return [args for args in found if step is None or args[args.index("sandbox-step") + 1] == step]

    def __call__(self, args, timeout, log):
        with self.lock:
            self.calls.append(list(args))
        command = args[1:3]
        if command == ["image", "inspect"]:
            if "--format" in args and self.image_id and log is not None:
                log.write(self.image_id.encode())
            return 0 if self.image else 1
        if command == ["volume", "inspect"]:
            return 0 if args[3] in self.volumes else 1
        if command == ["volume", "rm"]:
            self.volumes.pop(args[-1], None)
            return 0
        if args[1] == "kill":
            return 0
        if command == ["rm", "-f"]:
            self.created.pop(args[3], None)
            return 0
        if args[1] == "ps":
            label = args[-1].removeprefix("label=")
            log.write("".join(f"{name}\n" for name, created in self.created.items() if label in created).encode())
            return 0
        if args[1] == "create":
            name = args[args.index("--name") + 1]
            self.created[name] = list(args)
            if self.on_create is not None:
                self.on_create(name)
            return 0
        if command != ["start", "--attach"] or args[3] not in self.created:
            return 1
        args = self.created.pop(args[3])
        volume = mounts(args)["/deps"][1]
        self.volumes.setdefault(volume, False)
        if args[-3:] == ["test", "-f", MARKER]:
            return 0 if self.volumes[volume] else 1
        step = args[args.index("sandbox-step") + 1]
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if step in self.timeouts:
                raise subprocess.TimeoutExpired(args, timeout)
            time.sleep(self.delay)
            log.write(self.output or f"{step} output\n".encode())
            if step in self.hooks:
                self.hooks[step](args)
            if step == "install":
                self.volumes[volume] = self.install_exit == 0 and self.marker
                return self.install_exit
            out = Path(mounts(args)["/out"][1])
            if step == "e2e":
                qa = Path(mounts(args)["/qa"][1])
                self.qa_files = {
                    path.relative_to(qa).as_posix(): path.read_text() for path in qa.rglob("*") if path.is_file()
                }
                shot = out / "test-results" / "office-feature-тест-chromium" / "home.png"
                shot.parent.mkdir(parents=True)
                shot.write_bytes(b"\x89PNG")
                os.symlink("/etc/hosts", out / "link.txt")
            return self.exit_codes.get(step, 0)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def setup(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    client, me = office.login(app=app)
    docker = FakeDocker()

    def make(**overrides):
        settings = replace(app.state.settings, **overrides)
        return Sandbox(settings, app.state.store, app.state.workspace, runner=docker)

    def start(count=1):
        return office.start_tasks(app, me["id"], count)

    return app, client, docker, make, start


def ready(docker, sandbox, app, task_id):
    lock = app.state.workspace.worktree_path(task_id) / "apps/chat/web/package-lock.json"
    volume = sandbox.volume_prefix + hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    docker.volumes[volume] = True
    return volume


def expected_args(name, step, command, *, label, src, volume, out, network, deps_readonly=True, qa=None):
    args = [
        "docker", "create", "--rm", "--pull", "never", "--name", name,
        "--label", label,
        "--log-driver", "none",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=1g",
        "--tmpfs", "/work:rw,uid=10001,gid=10001,mode=0755,size=1g",
        "--memory", "3g",
        "--cpus", "2",
        "--pids-limit", "1024",
        "--env", "CI=true",
        "--env", "HOME=/tmp",
    ]
    if not network:
        args += ["--network", "none"]
    args += ["--mount", f"type=bind,source={src},target=/src,readonly"]
    args += ["--mount", f"type=volume,source={volume},target=/deps" + (",readonly" if deps_readonly else "")]
    if out:
        args += ["--mount", f"type=bind,source={out},target=/out"]
    if qa:
        args += ["--mount", f"type=bind,source={qa},target=/qa,readonly"]
    return args + ["back-office-sandbox:p4", "sandbox-step", step, command]


def test_docker_arguments_for_install_and_check(setup, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "office-secret-value")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/agent.sock")
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    src = app.state.workspace.worktree_path(task_id) / "apps/chat"
    result = sandbox.run_step(task_id, "lint", AgentRole.developer)
    assert result.exit_code == 0
    [install] = docker.runs("install")
    [lint] = docker.runs("lint")
    volume = mounts(install)["/deps"][1]
    assert volume.startswith("office-nm-") and len(volume) == len("office-nm-") + 16
    label = sandbox.label
    assert label.startswith("back-office.workspace=") and len(label) == len("back-office.workspace=") + 16
    assert install == expected_args(
        f"office-{task_id}-install-1", "install", "npm ci --no-audit --no-fund",
        label=label, src=src, volume=volume, out=None, network=True, deps_readonly=False,
    )
    assert lint == expected_args(
        f"office-{task_id}-lint-1", "lint", "npm run lint",
        label=label, src=src, volume=volume, out=mounts(lint)["/out"][1], network=False,
    )
    for args in docker.calls:
        text = " ".join(args)
        assert "office-secret-value" not in text
        assert "SSH_AUTH_SOCK" not in text and "agent.sock" not in text
        assert "docker.sock" not in text
        assert str(Path.home()) not in text
        assert "-v" not in args and "--privileged" not in args
    assert result.command == "npm run lint"
    assert result.artifact_urls == [f"/api/tasks/{task_id}/artifacts/lint-1.log"]


@pytest.mark.parametrize(("step", "role"), [("e2e", AgentRole.developer), ("lint", AgentRole.qa),
                                             ("install", AgentRole.developer), ("rm -rf /", AgentRole.developer),
                                             ("build", AgentRole.critic)])
def test_step_outside_policy_is_denied(setup, step, role):
    app, _, docker, make, start = setup
    [task_id] = start()
    with pytest.raises(StepDenied):
        make().run_step(task_id, step, role)
    assert docker.calls == []
    event = app.state.store.events_after(task_id, 0, 1000)[-1]
    assert (event.type, event.tool, event.role) == ("tool_called", "run_check", role)
    assert event.payload["result"] == "denied"


def test_missing_image_is_reported(setup):
    _, _, docker, make, start = setup
    [task_id] = start()
    docker.image = False
    with pytest.raises(SandboxError, match=UNAVAILABLE):
        make().run_step(task_id, "lint", AgentRole.developer)


def test_at_most_two_containers_run_at_once(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    ids = start(3)
    ready(docker, sandbox, app, ids[0])
    docker.delay = 0.3
    threads = [threading.Thread(target=sandbox.run_step, args=(task_id, "test", AgentRole.developer))
               for task_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(docker.runs("test")) == 3
    assert docker.max_running == 2


def test_timeout_kills_container(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.timeouts.add("build")
    result = sandbox.run_step(task_id, "build", AgentRole.developer)
    assert result.exit_code == 124
    assert ["docker", "kill", f"office-{task_id}-build-1"] in docker.calls
    event = app.state.store.events_after(task_id, 0, 1000)[-1]
    assert event.type == "check_result"
    assert event.payload["exit_code"] == 124


def test_same_lock_file_installs_once(setup):
    _, _, docker, make, start = setup
    sandbox = make()
    ids = start(2)
    docker.delay = 0.3
    threads = [threading.Thread(target=sandbox.run_step, args=(task_id, "lint", AgentRole.developer))
               for task_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(docker.runs("install")) == 1
    assert len(docker.runs("lint")) == 2
    first_lint = docker.calls.index(docker.runs("lint")[0])
    assert docker.calls.index(docker.runs("install")[0]) < first_lint


def test_failed_install_removes_volume_and_retries(setup):
    _, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    docker.install_exit = 1
    result = sandbox.run_step(task_id, "lint", AgentRole.developer)
    assert result.exit_code == 1
    assert result.command == "npm ci --no-audit --no-fund"
    assert docker.runs("lint") == []
    volume = mounts(docker.runs("install")[0])["/deps"][1]
    assert volume not in docker.volumes
    assert docker.calls[-1] == ["docker", "volume", "rm", "-f", volume]
    docker.install_exit = 0
    assert sandbox.run_step(task_id, "lint", AgentRole.developer).exit_code == 0
    assert len(docker.runs("install")) == 2


def test_volume_without_marker_is_not_mounted(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    volume = ready(docker, sandbox, app, task_id)
    docker.volumes[volume] = False
    sandbox.run_step(task_id, "lint", AgentRole.developer)
    removal = docker.calls.index(["docker", "volume", "rm", "-f", volume])
    install = docker.calls.index(docker.runs("install")[0])
    lint = docker.calls.index(docker.runs("lint")[0])
    assert removal < install < lint


def test_suite_report_and_artifacts(setup):
    app, client, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.exit_codes["test"] = 1
    base = app.state.store.get_task(task_id).base_sha
    report = sandbox.run_suite(task_id, base, base)
    assert report.commit_sha == base
    assert report.spec_hash is None
    assert report.suite_hash.startswith("sha256:")
    assert [check.command for check in report.checks] == ["npm run lint", "npm run test", "npm run build"]
    assert [check.exit_code for check in report.checks] == [0, 1, 0]
    assert client.get(f"/api/tasks/{task_id}/checks").json() == report.model_dump(mode="json")
    log = client.get(f"/api/tasks/{task_id}/artifacts/test-1.log")
    assert log.status_code == 200
    assert log.text == "test output\n"
    events = [event for event in app.state.store.events_after(task_id, 0, 1000) if event.type == "check_result"]
    assert [event.payload["step"] for event in events] == ["lint", "test", "build"]
    assert all(event.payload["commit_sha"] == base and event.payload["qa_sha"] == base for event in events)
    with pytest.raises(SandboxError, match="QA"):
        sandbox.run_suite(task_id, base, base, require_e2e=True)


def test_suite_runs_same_qa_on_base_and_task_commit(setup):
    app, client, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "apps/chat/web/e2e/office").mkdir(parents=True)
    (root / "apps/chat/web/e2e/office/feature.pw.ts").write_text("test('feature')\n")
    (root / "apps/chat/web/src/Feature.tsx").write_text("export {}\n")
    commit = workspace.commit(task_id, "feature")
    base = app.state.store.get_task(task_id).base_sha

    before = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    first_qa = docker.qa_files
    [first_e2e] = docker.runs("e2e")
    assert mounts(first_e2e)["/src"][1] != str(root)
    after = sandbox.run_suite(task_id, commit, commit, require_e2e=True)
    second_e2e = docker.runs("e2e")[1]
    assert mounts(second_e2e)["/src"][1] != str(root)
    assert first_qa == docker.qa_files == {"apps/chat/web/e2e/office/feature.pw.ts": "test('feature')\n"}
    assert before.commit_sha == base and after.commit_sha == commit
    assert before.suite_hash == after.suite_hash
    assert [check.command for check in after.checks][-1].startswith("playwright test")
    assert not any(path.name.startswith(".suite-") for path in workspace.root.iterdir())

    names = [url.rsplit("/", 1)[1] for url in after.checks[-1].artifact_urls]
    assert names[0] == "e2e-2.log"
    [shot] = [name for name in names if name.endswith(".png")]
    assert shot.startswith("e2e-2-") and "link" not in "".join(names)
    response = client.get(f"/api/tasks/{task_id}/artifacts/{shot}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_suite_without_required_e2e_skips_existing_qa_test(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "apps/chat/web/e2e/office").mkdir(parents=True)
    (root / "apps/chat/web/e2e/office/old.pw.ts").write_text("test('old attempt')\n")
    commit = workspace.commit(task_id, "qa test of previous attempt")
    docker.exit_codes["e2e"] = 1

    report = sandbox.run_suite(task_id, commit, commit)
    assert [check.command for check in report.checks] == ["npm run lint", "npm run test", "npm run build"]
    assert docker.runs("e2e") == []
    full = sandbox.run_suite(task_id, commit, commit, require_e2e=True)
    assert [check.exit_code for check in full.checks] == [0, 0, 0, 1]
    assert report.suite_hash != full.suite_hash


def test_suite_hash_covers_base_tests_of_chat_app(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    root = app.state.workspace.worktree_path(task_id)
    base = git_head(root)
    (root / "apps/chat/web/src/test/setup.ts").write_text("export const changed = 1\n")
    git(root, "commit", "-q", "--no-verify", "-am", "base test")
    changed = git_head(root)
    hashes = [sandbox.run_suite(task_id, sha, sha).suite_hash for sha in (base, changed)]
    assert hashes[0] != hashes[1]


def test_suite_checks_committed_content_while_worktree_changes(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "apps/chat/web/src/App.tsx").write_text("committed\n")
    commit = workspace.commit(task_id, "feature")
    (root / "apps/chat/web/src/Draft.tsx").write_text("draft\n")
    seen = {}

    def edit_worktree(args):
        (root / "apps/chat/web/src/App.tsx").write_text("changed between steps\n")

    def read_source(args):
        src = Path(mounts(args)["/src"][1])
        seen["app"] = (src / "web/src/App.tsx").read_text()
        seen["draft"] = (src / "web/src/Draft.tsx").exists()
        seen["src"] = src

    docker.hooks = {"lint": edit_worktree, "build": read_source}
    report = sandbox.run_suite(task_id, git_head(root), git_head(root))
    assert report.commit_sha == commit
    assert seen["app"] == "committed\n"
    assert seen["draft"] is False
    assert seen["src"] != root
    assert len({mounts(args)["/src"][1] for args in docker.runs()}) == 1


def creates(docker, start=0):
    return [args for args in docker.calls[start:] if args[:2] == ["docker", "create"]]


def check_events(app, task_id):
    return [event for event in app.state.store.events_after(task_id, 0, 1000) if event.type == "check_result"]


def test_developer_checks_are_reused_by_suite_on_same_commit(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.image_id = "sha256:image-1"
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "apps/chat/web/src/Feature.tsx").write_text("export const feature = 1\n")
    docker.exit_codes["test"] = 1
    own = [sandbox.run_step(task_id, step, AgentRole.developer) for step in ("lint", "test", "build")]
    commit = workspace.commit(task_id, "feature")
    before = len(docker.calls)

    report = sandbox.run_suite(task_id, commit, commit)
    assert creates(docker, before) == []
    assert report.checks == own
    assert [check.exit_code for check in report.checks] == [0, 1, 0]
    events = check_events(app, task_id)
    assert all("cached" not in event.payload for event in events[:3])
    assert [event.payload["step"] for event in events[3:]] == ["lint", "test", "build"]
    assert all(event.payload["cached"] is True and event.payload["commit_sha"] == commit for event in events[3:])
    assert [event.artifact for event in events[3:]] == ["lint-1.log", "test-1.log", "build-1.log"]
    assert [event.payload["duration_ms"] for event in events[3:]] == [check.duration_ms for check in own]
    assert app.state.store.check_reports(task_id, commit)[0].checks == own


def test_worktree_with_ignored_files_is_not_reused_for_commit(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.image_id = "sha256:image-1"
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (workspace.clone / ".git/info/exclude").write_text("coverage/\n")
    (root / "apps/chat/web/src/coverage").mkdir()
    (root / "apps/chat/web/src/coverage/helper.ts").write_text("export const helper = 1\n")
    (root / "apps/chat/web/src/App.tsx").write_text("import { helper } from './coverage/helper'\nexport const app = helper\n")
    sandbox.run_step(task_id, "build", AgentRole.developer)
    sandbox.run_step(task_id, "build", AgentRole.developer)
    assert len(docker.runs("build")) == 2
    commit = workspace.commit(task_id, "feature")
    assert "apps/chat/web/src/coverage/helper.ts" not in git(root, "ls-tree", "-r", "--name-only", commit)
    sandbox.run_suite(task_id, commit, commit)
    assert len(docker.runs("build")) == 3
    assert all("cached" not in event.payload for event in check_events(app, task_id))


@pytest.mark.parametrize("change", ["source", "qa", "lock", "policy", "image"])
def test_changed_input_runs_check_again(setup, tmp_path, change):
    app, _, docker, make, start = setup
    policy = tmp_path / "commands.yaml"
    policy.write_text(app.state.settings.commands_policy.read_text())
    sandbox = make(commands_policy=policy)
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.image_id = "sha256:image-1"
    root = app.state.workspace.worktree_path(task_id)
    qa_test = root / "apps/chat/web/e2e/office/feature.pw.ts"
    qa_test.parent.mkdir(parents=True)
    qa_test.write_text("test('feature')\n")
    step, role = ("e2e", AgentRole.qa) if change == "qa" else ("lint", AgentRole.developer)
    sandbox.run_step(task_id, step, role)
    sandbox.run_step(task_id, step, role)
    assert len(docker.runs(step)) == 1
    if change == "source":
        (root / "apps/chat/web/src/App.tsx").write_text("export const changed = 1\n")
    elif change == "qa":
        qa_test.write_text("test('changed')\n")
    elif change == "lock":
        lock = root / "apps/chat/web/package-lock.json"
        lock.write_text(lock.read_text() + "\n")
    elif change == "policy":
        policy.write_text(policy.read_text() + "\n# changed\n")
    else:
        docker.image_id = "sha256:image-2"
    sandbox.run_step(task_id, step, role)
    assert len(docker.runs(step)) == 2


def test_failure_is_cached_but_timeout_is_not(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.image_id = "sha256:image-1"
    docker.timeouts = {"lint"}
    assert sandbox.run_step(task_id, "lint", AgentRole.developer).exit_code == 124
    docker.timeouts = set()
    docker.exit_codes["lint"] = 1
    assert sandbox.run_step(task_id, "lint", AgentRole.developer).exit_code == 1
    assert sandbox.run_step(task_id, "lint", AgentRole.developer).exit_code == 1
    assert len(docker.runs("lint")) == 2


def test_checks_are_not_reused_without_image_id_or_across_tasks(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    first, second = start(2)
    for task_id in (first, second):
        ready(docker, sandbox, app, task_id)
    sandbox.run_step(first, "lint", AgentRole.developer)
    sandbox.run_step(first, "lint", AgentRole.developer)
    assert len(docker.runs("lint")) == 2
    docker.image_id = "sha256:image-1"
    sandbox.run_step(first, "lint", AgentRole.developer)
    sandbox.run_step(second, "lint", AgentRole.developer)
    assert len(docker.runs("lint")) == 4
    sandbox.run_step(first, "lint", AgentRole.developer)
    sandbox.run_step(second, "lint", AgentRole.developer)
    assert len(docker.runs("lint")) == 4


def test_repeated_qa_run_on_base_reuses_result_unless_fresh(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.image_id = "sha256:image-1"
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "apps/chat/web/e2e/office").mkdir(parents=True)
    (root / f"apps/chat/{QA_FILE}").write_text("test('feature')\n")
    commit = workspace.commit(task_id, "qa test")
    base = app.state.store.get_task(task_id).base_sha

    def write_report(args):
        out = Path(mounts(args)["/out"][1])
        (out / "playwright-report.json").write_text(json.dumps(report(result("failed", FAILURE))))

    docker.hooks = {"e2e": write_report}
    first = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    second = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    assert len(docker.runs()) == 4
    assert second.checks == first.checks
    assert sandbox.e2e_result(task_id, second, QA_FILE).status == "failed"
    assert sandbox.e2e_result(task_id, second, QA_FILE) == sandbox.e2e_result(task_id, first, QA_FILE)

    fresh = sandbox.run_suite(task_id, base, commit, require_e2e=True, reuse=False)
    assert len(docker.runs()) == 8
    assert fresh.checks[-1].artifact_urls != first.checks[-1].artifact_urls
    again = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    assert len(docker.runs()) == 8
    assert again.checks == fresh.checks


def test_missing_marker_after_install_stops_checks(setup):
    _, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    docker.marker = False
    with pytest.raises(SandboxError, match="готовность"):
        sandbox.run_step(task_id, "lint", AgentRole.developer)
    volume = mounts(docker.runs("install")[0])["/deps"][1]
    assert docker.runs("lint") == []
    assert docker.calls[-1] == ["docker", "volume", "rm", "-f", volume]


def test_log_is_capped(setup):
    app, client, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.output = b"x" * (sandbox.policy.log_bytes + 1000)
    sandbox.run_step(task_id, "lint", AgentRole.developer)
    log = app.state.settings.artifacts_dir / task_id / "lint-1.log"
    data = log.read_bytes()
    assert data.startswith(b"x" * sandbox.policy.log_bytes)
    assert data[sandbox.policy.log_bytes:] == "\n[журнал обрезан]\n".encode()


def test_process_output_is_capped_while_running(tmp_path):
    with open(tmp_path / "out.log", "wb") as file:
        log = CappedLog(file, 1000)
        with pytest.raises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", "while True: print('x' * 1000, flush=True)"], 1, log)
    assert log.truncated
    assert (tmp_path / "out.log").stat().st_size == 1000


@pytest.mark.parametrize("value", [0, 3])
def test_container_limit_is_validated(setup, value):
    _, _, _, make, _ = setup
    with pytest.raises(ValueError, match="OFFICE_SANDBOX_CONTAINERS"):
        make(sandbox_containers=value)


def test_empty_qa_set_is_error_for_e2e_step(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    with pytest.raises(SandboxError, match="QA"):
        sandbox.run_step(task_id, "e2e", AgentRole.qa)
    assert docker.runs("e2e") == []


def test_cancel_during_suite_removes_container_and_skips_next_steps(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    docker.hooks = {"lint": lambda args: sandbox.cancel_task(task_id)}
    base = app.state.store.get_task(task_id).base_sha
    with pytest.raises(SandboxCancelled):
        sandbox.run_suite(task_id, base, base)
    assert ["docker", "rm", "-f", f"office-{task_id}-lint-1"] in docker.calls
    assert docker.runs("test") == [] and docker.runs("build") == []
    assert not sandbox.has_containers(task_id)
    with pytest.raises(SandboxCancelled):
        sandbox.run_step(task_id, "lint", AgentRole.developer)
    sandbox.reset_task(task_id)
    docker.hooks = {}
    assert sandbox.run_step(task_id, "lint", AgentRole.developer).exit_code == 0


def test_cancel_while_waiting_for_container_slot(setup):
    app, _, docker, make, start = setup
    sandbox = make(sandbox_containers=1)
    first, second = start(2)
    ready(docker, sandbox, app, first)
    running = threading.Event()
    release = threading.Event()

    def hold(args):
        running.set()
        release.wait(10)

    docker.hooks = {"lint": hold}
    errors = []

    def run(task_id):
        try:
            sandbox.run_step(task_id, "lint", AgentRole.developer)
        except SandboxCancelled as error:
            errors.append((task_id, error))

    threads = [threading.Thread(target=run, args=(first,))]
    threads[0].start()
    assert running.wait(10)
    threads.append(threading.Thread(target=run, args=(second,)))
    threads[1].start()
    time.sleep(0.2)
    sandbox.cancel_task(second)
    mark = len(docker.calls)
    release.set()
    for thread in threads:
        thread.join()
    assert [task_id for task_id, _ in errors] == [second]
    assert [args for args in docker.calls[mark:] if args[1] == "create"] == []
    assert [args[args.index("--name") + 1] for args in docker.runs("lint")] == [f"office-{first}-lint-1"]


def test_cancel_during_create_removes_container_without_start(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    name = f"office-{task_id}-lint-1"
    threads = []

    def on_create(created):
        if created == name:
            thread = threading.Thread(target=sandbox.cancel_task, args=(task_id,))
            thread.start()
            threads.append(thread)
            wait_for(lambda: sandbox.cancelled(task_id), 10, "cancel flag")

    docker.on_create = on_create
    with pytest.raises(SandboxCancelled):
        sandbox.run_step(task_id, "lint", AgentRole.developer)
    for thread in threads:
        thread.join()
    assert ["docker", "start", "--attach", name] not in docker.calls
    assert ["docker", "rm", "-f", name] in docker.calls
    assert name not in docker.created
    assert not sandbox.has_containers(task_id)


def test_stale_containers_of_this_workspace_are_removed(setup):
    app, _, docker, make, _ = setup
    sandbox = make()
    other = Sandbox(app.state.settings, app.state.store, SimpleNamespace(root=Path("/elsewhere")), runner=docker)
    docker.created = {
        "office-tsk_old-lint-1": ["docker", "create", "--label", sandbox.label],
        "office-probe-0001": ["docker", "create", "--label", sandbox.label],
        "office-pg-test": ["docker", "create"],
    }
    sandbox.remove_stale()
    assert ["docker", "ps", "-aq", "--filter", f"label={sandbox.label}"] in docker.calls
    assert set(docker.created) == {"office-pg-test"}
    assert other.label != sandbox.label


QA_FILE = "web/e2e/office/tsk_1.pw.ts"


def report(*tests, errors=(), file=QA_FILE):
    return {
        "errors": list(errors),
        "suites": [{"title": file, "file": file, "specs": [], "suites": [
            {"title": "group", "file": file, "specs": [{"title": "t", "file": file, "tests": list(tests)}]}
        ]}],
    }


def result(status, *errors):
    return {"status": "expected" if status == "passed" else "unexpected", "results": [{"status": status, "errors": list(errors)}]}


FAILURE = {"message": "\x1b[31mError: expect(locator).toBeVisible() failed\x1b[39m", "location": {"file": f"/qa/{QA_FILE}", "line": 12, "column": 5}}
TIMEOUT = {"message": "Test timeout of 30000ms exceeded."}
LOCATOR = {"message": "Error: locator.click: Timeout 1000ms exceeded.", "location": {"file": f"/qa/{QA_FILE}", "line": 9, "column": 3}}


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, "no_evidence"),
        (report(result("passed"), errors=[{"message": "webServer exited"}]), "no_evidence"),
        (report(result("failed", FAILURE), file="web/e2e/office/other.pw.ts"), "no_evidence"),
        (report(), "no_evidence"),
        (report(result("passed"), {"status": "skipped", "results": [{"status": "skipped", "errors": []}]}), "no_evidence"),
        (report(result("interrupted")), "no_evidence"),
        (report(result("failed")), "no_evidence"),
        (report(result("passed"), result("passed")), "passed"),
        (report(result("failed", FAILURE)), "failed"),
        (report(result("failed", {"message": "Error: expect(received).toBe(expected) // Object.is equality"})), "failed"),
        (report(result("failed", {"message": "Error: Timed out 5000ms waiting for expect(locator).toHaveText(expected)"})), "failed"),
        (report(result("timedOut", TIMEOUT, FAILURE)), "failed"),
        (report(result("timedOut", TIMEOUT)), "no_evidence"),
        (report(result("failed", {"message": "Error: browserType.launch: Executable doesn't exist at /ms-playwright/chromium"})), "no_evidence"),
        (report(result("failed", {"message": "Error: page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:4173/"})), "no_evidence"),
        (report(result("failed", {"message": "Error: Fixture \"page\" timeout of 30000ms exceeded during setup."})), "no_evidence"),
        (report(result("failed", FAILURE), result("failed", {"message": "Error: page.goto: net::ERR_ABORTED"})), "no_evidence"),
        (report(result("failed", LOCATOR)), "test_error"),
        (report(result("timedOut", TIMEOUT, LOCATOR)), "test_error"),
        (report(result("failed", FAILURE), result("failed", LOCATOR)), "test_error"),
        (report(result("failed", FAILURE, LOCATOR)), "test_error"),
        (report(result("failed", FAILURE, TIMEOUT)), "failed"),
        (report(result("failed", LOCATOR | {"location": {"file": "/qa/web/e2e/office/other.pw.ts", "line": 3}})), "no_evidence"),
        (report(result("failed", {"message": "Error: Test timeout of 30000ms exceeded while setting up \"page\".", "location": LOCATOR["location"]})), "no_evidence"),
    ],
)
def test_playwright_report_parsing(data, expected):
    assert parse_playwright(data, QA_FILE).status == expected


PLAYWRIGHT_REPORTS = Path(__file__).parent / "data" / "playwright"


@pytest.mark.parametrize(
    ("name", "status", "line", "text"),
    [
        ("strict", "test_error", 7, "strict mode violation: getByRole('button', { name: 'Кот в скафандре' }) resolved to 2 elements"),
        ("missing", "test_error", 5, "locator.click: Timeout 1000ms exceeded."),
        ("timeout", "test_error", 5, "locator.click: Test timeout of 5000ms exceeded."),
        ("throw", "test_error", 6, "Error: Список диалогов не задан"),
        ("helper", "test_error", 6, "Error: В подмене нет диалогов"),
        ("assertion", "failed", 5, "toBeVisible() failed"),
        ("noserver", "no_evidence", None, ""),
    ],
)
def test_real_playwright_reports(name, status, line, text):
    data = json.loads((PLAYWRIGHT_REPORTS / f"{name}.json").read_text(encoding="utf-8"))
    parsed = parse_playwright(data, f"apps/chat/web/e2e/office/{name}.pw.ts")
    assert (parsed.status, parsed.line) == (status, line)
    assert text in parsed.message
    assert "\x1b" not in parsed.message


def test_test_error_message_is_cut():
    long = LOCATOR | {"message": "\x1b[31mError: locator.click: " + "x" * 3000}
    parsed = parse_playwright(report(result("failed", long)), QA_FILE)
    assert parsed.status == "test_error" and parsed.line == 9
    assert len(parsed.message) == 1500 and parsed.message.startswith("Error: locator.click: x")


def test_locator_error_after_soft_assertion_in_same_result_is_test_error():
    soft = FAILURE | {"message": "Error: expect.soft(locator).toBeVisible() failed"}
    strict = {
        "message": "Error: locator.click: Error: strict mode violation: getByRole('button') resolved to 2 elements",
        "location": {"file": f"/qa/{QA_FILE}", "line": 14, "column": 3},
    }
    parsed = parse_playwright(report(result("failed", soft, strict)), QA_FILE)
    assert (parsed.status, parsed.line) == ("test_error", 14)
    assert "strict mode violation" in parsed.message


def test_playwright_failure_details():
    parsed = parse_playwright(report(result("passed"), result("failed", FAILURE)), QA_FILE)
    assert (parsed.status, parsed.line) == ("failed", 12)
    assert parsed.message == "Error: expect(locator).toBeVisible() failed"


OTHER_FILE = "web/e2e/office/tsk_0.pw.ts"


def two_files(own, other, other_file=OTHER_FILE):
    data = report(own)
    data["suites"].append(report(other, file=other_file)["suites"][0])
    return data


def test_failed_tests_lists_failures_of_other_files():
    assert failed_tests(two_files(result("passed"), result("passed")), QA_FILE) == []
    expected = [{"file": OTHER_FILE, "title": "t", "message": "Error: expect(locator).toBeVisible() failed"}]
    assert failed_tests(two_files(result("passed"), result("failed", FAILURE)), QA_FILE) == expected
    assert failed_tests(two_files(result("passed"), result("failed", FAILURE), f"../{OTHER_FILE}"), QA_FILE) == expected
    timed_out = failed_tests(two_files(result("failed", FAILURE), result("timedOut", TIMEOUT)), QA_FILE)
    assert timed_out == [{"file": OTHER_FILE, "title": "t", "message": "Test timeout of 30000ms exceeded."}]
    assert [item["file"] for item in failed_tests(two_files(result("failed", FAILURE), result("timedOut", TIMEOUT)))] == [
        QA_FILE, OTHER_FILE
    ]
    assert failed_tests(two_files(result("passed"), result("failed", FAILURE), "web/e2e/office/support/x.ts"), QA_FILE) == []
    assert failed_tests(None, QA_FILE) == []


def test_other_failures_reads_report_artifact(setup):
    app, _, _, make, start = setup
    sandbox = make()
    [task_id] = start()
    command = sandbox.policy.steps["e2e"].command
    directory = app.state.settings.artifacts_dir / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "e2e-1-0-playwright-report.json").write_text(json.dumps(two_files(result("passed"), result("failed", FAILURE))))
    urls = [f"/api/tasks/{task_id}/artifacts/e2e-1.log", f"/api/tasks/{task_id}/artifacts/e2e-1-0-playwright-report.json"]

    def suite(artifact_urls, checks=None):
        check = CheckResult(command=command, exit_code=1, duration_ms=1, artifact_urls=artifact_urls)
        return CheckReport(task_id=task_id, commit_sha="a" * 40, spec_hash=None, suite_hash=None, checks=checks or [check])

    [found] = sandbox.other_failures(task_id, suite(urls), QA_FILE)
    assert (found["file"], found["title"]) == (OTHER_FILE, "t")
    assert sandbox.e2e_result(task_id, suite(urls), QA_FILE).status == "passed"
    assert sandbox.other_failures(task_id, suite(urls[:1]), QA_FILE) == []
    assert sandbox.other_failures(task_id, suite(urls), "apps/chat/api/tests/office/test_tsk_1.py") == []
    lint = CheckResult(command="npm run lint", exit_code=1, duration_ms=1, artifact_urls=[])
    assert sandbox.other_failures(task_id, suite(urls, [lint]), QA_FILE) == []


def test_e2e_result_reads_report_artifact(setup):
    app, _, _, make, start = setup
    sandbox = make()
    [task_id] = start()
    command = sandbox.policy.steps["e2e"].command
    directory = app.state.settings.artifacts_dir / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "e2e-1-0-playwright-report.json").write_text(json.dumps(report(result("failed", FAILURE))))
    urls = [f"/api/tasks/{task_id}/artifacts/e2e-1.log", f"/api/tasks/{task_id}/artifacts/e2e-1-0-playwright-report.json"]

    def outcome(exit_code, artifact_urls=urls):
        check = CheckResult(command=command, exit_code=exit_code, duration_ms=1, artifact_urls=artifact_urls)
        suite = CheckReport(task_id=task_id, commit_sha="a" * 40, spec_hash=None, suite_hash=None, checks=[check])
        return sandbox.e2e_result(task_id, suite, QA_FILE)

    assert (outcome(1).status, outcome(1).line) == ("failed", 12)
    assert outcome(90).status == "no_evidence" and "Сборка" in outcome(90).reason
    assert outcome(124).status == "no_evidence"
    assert outcome(1, urls[:1]).status == "no_evidence"
    empty = CheckReport(task_id=task_id, commit_sha=None, spec_hash=None, suite_hash=None, checks=[])
    assert sandbox.e2e_result(task_id, empty, QA_FILE).status == "no_evidence"

    lines = "".join(f"GET /api/item/{index}\n" for index in range(7)) + "GET /api/item/0\n"
    (directory / "e2e-1-1-unmocked-api.txt").write_text(lines)
    unmocked = urls + [f"/api/tasks/{task_id}/artifacts/e2e-1-1-unmocked-api.txt"]
    for exit_code in (0, 1):
        invalid = outcome(exit_code, unmocked)
        assert invalid.status == "invalid" and invalid.message == ""
        assert "GET /api/item/0, GET /api/item/1, GET /api/item/2, GET /api/item/3, GET /api/item/4 и ещё 2" in invalid.reason
        assert "page.route" in invalid.reason
    assert outcome(90, unmocked).status == "no_evidence"
    assert outcome(1, urls[:1] + unmocked[2:]).status == "invalid"


def test_unmocked_requests_reads_artifact(tmp_path):
    assert unmocked_requests(tmp_path, ["/api/tasks/t/artifacts/e2e-1.log"]) == []
    assert unmocked_requests(tmp_path, ["/api/tasks/t/artifacts/e2e-1-0-unmocked-api.txt"]) == ["список запросов недоступен"]
    (tmp_path / "e2e-1-0-unmocked-api.txt").write_text("POST /api/demo-session\n\nPOST /api/demo-session\nGET /healthz\n")
    found = unmocked_requests(tmp_path, ["/api/tasks/t/artifacts/e2e-1-0-unmocked-api.txt"])
    assert found == ["POST /api/demo-session", "GET /healthz"]
    assert unmocked_reason(found).startswith("Тест QA обратился к API vibe-chat без подмены: POST /api/demo-session, GET /healthz.")
