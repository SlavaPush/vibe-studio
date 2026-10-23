import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import git

from app.sandbox import MARKER, UNAVAILABLE, CappedLog, Sandbox, SandboxError, StepDenied, run_process
from app.schemas import AgentRole


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
        self.lock = threading.Lock()

    def runs(self, step=None):
        found = [args for args in self.calls if args[:2] == ["docker", "run"] and "sandbox-step" in args]
        return [args for args in found if step is None or args[args.index("sandbox-step") + 1] == step]

    def __call__(self, args, timeout, log):
        with self.lock:
            self.calls.append(list(args))
        command = args[1:3]
        if command == ["image", "inspect"]:
            return 0 if self.image else 1
        if command == ["volume", "inspect"]:
            return 0 if args[3] in self.volumes else 1
        if command == ["volume", "rm"]:
            self.volumes.pop(args[-1], None)
            return 0
        if args[1] == "kill":
            return 0
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
    lock = app.state.workspace.worktree_path(task_id) / "web" / "package-lock.json"
    volume = sandbox.volume_prefix + hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    docker.volumes[volume] = True
    return volume


def expected_args(name, step, command, *, src, volume, out, network, deps_readonly=True, qa=None):
    args = [
        "docker", "run", "--rm", "--pull", "never", "--name", name,
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
    src = app.state.workspace.worktree_path(task_id)
    result = sandbox.run_step(task_id, "lint", AgentRole.developer)
    assert result.exit_code == 0
    [install] = docker.runs("install")
    [lint] = docker.runs("lint")
    volume = mounts(install)["/deps"][1]
    assert volume.startswith("office-nm-") and len(volume) == len("office-nm-") + 16
    assert install == expected_args(
        f"office-{task_id}-install-1", "install", "npm ci --no-audit --no-fund",
        src=src, volume=volume, out=None, network=True, deps_readonly=False,
    )
    assert lint == expected_args(
        f"office-{task_id}-lint-1", "lint", "npm run lint",
        src=src, volume=volume, out=mounts(lint)["/out"][1], network=False,
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
    (root / "web/e2e/office").mkdir(parents=True)
    (root / "web/e2e/office/feature.pw.ts").write_text("test('feature')\n")
    (root / "web/src/Feature.tsx").write_text("export {}\n")
    commit = workspace.commit(task_id, "feature")
    base = app.state.store.get_task(task_id).base_sha

    before = sandbox.run_suite(task_id, base, commit, require_e2e=True)
    first_qa = docker.qa_files
    [first_e2e] = docker.runs("e2e")
    assert mounts(first_e2e)["/src"][1] != str(root)
    after = sandbox.run_suite(task_id, commit, commit, require_e2e=True)
    second_e2e = docker.runs("e2e")[1]
    assert mounts(second_e2e)["/src"][1] != str(root)
    assert first_qa == docker.qa_files == {"web/e2e/office/feature.pw.ts": "test('feature')\n"}
    assert before.commit_sha == base and after.commit_sha == commit
    assert before.suite_hash == after.suite_hash
    assert [check.command for check in after.checks][-1].startswith("npm run build && playwright test")
    assert not any(path.name.startswith(".suite-") for path in workspace.root.iterdir())

    names = [url.rsplit("/", 1)[1] for url in after.checks[-1].artifact_urls]
    assert names[0] == "e2e-2.log"
    [shot] = [name for name in names if name.endswith(".png")]
    assert shot.startswith("e2e-2-") and "link" not in "".join(names)
    response = client.get(f"/api/tasks/{task_id}/artifacts/{shot}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_suite_checks_committed_content_while_worktree_changes(setup):
    app, _, docker, make, start = setup
    sandbox = make()
    [task_id] = start()
    ready(docker, sandbox, app, task_id)
    workspace = app.state.workspace
    root = workspace.worktree_path(task_id)
    (root / "web/src/App.tsx").write_text("committed\n")
    commit = workspace.commit(task_id, "feature")
    (root / "web/src/Draft.tsx").write_text("draft\n")
    seen = {}

    def edit_worktree(args):
        (root / "web/src/App.tsx").write_text("changed between steps\n")

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
