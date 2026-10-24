import hashlib
import os
import threading
from pathlib import Path

import pytest

from app import python_sandbox
from app.python_sandbox import PythonSandbox
from app.sandbox import MARKER, Sandbox, SandboxCancelled, SandboxError

PYPROJECT = '[project]\nname = "vibe-chat-api"\n'
LOCK = "version = 1\n"


def mounts(args):
    result = {}
    for index, value in enumerate(args):
        if value == "--mount":
            fields = dict(item.split("=", 1) for item in args[index + 1].split(",") if "=" in item)
            result[fields["target"]] = (fields["type"], fields["source"], args[index + 1].endswith(",readonly"))
    return result


def option(args, flag):
    return [args[index + 1] for index, value in enumerate(args) if value == flag]


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.images = {"office-python-sandbox:p5", "postgres:16-alpine"}
        self.volumes = {}
        self.networks = set()
        self.created = {}
        self.started = set()
        self.install_exit = 0
        self.test_exit = 0
        self.marker = True
        self.pg_ready = 0
        self.failures = {}
        self.on_create = None
        self.on_install = None
        self.on_test = None
        self.lock = threading.Lock()

    def create_args(self, kind):
        return [
            args for args in self.calls
            if args[:2] == ["docker", "create"] and args[args.index("--name") + 1].split("-")[-2:-1] == [kind]
        ]

    def step_args(self, step):
        return [args for args in self.calls if args[:2] == ["docker", "create"] and f"-{step}-" in args[args.index("--name") + 1]]

    def __call__(self, args, timeout, log):
        with self.lock:
            self.calls.append(list(args))
        command = args[1:3]
        if command == ["image", "inspect"]:
            return 0 if args[3] in self.images else 1
        if command == ["volume", "inspect"]:
            return 0 if args[3] in self.volumes else 1
        if command == ["volume", "rm"]:
            self.volumes.pop(args[-1], None)
            return 0
        if command == ["network", "create"]:
            self.networks.add(args[-1])
            return 0
        if command == ["network", "rm"]:
            self.networks.discard(args[-1])
            return 0
        if args[1] in ("ps", "network") and log is not None:
            label = args[-1].removeprefix("label=")
            if args[1] == "ps":
                items = [name for name, created in self.created.items() if label in created]
            else:
                items = sorted(self.networks)
            log.write("".join(f"{item}\n" for item in items).encode())
            return 0
        if args[1] == "kill":
            return 0
        if command == ["rm", "-f"]:
            self.created.pop(args[3], None)
            self.started.discard(args[3])
            return 0
        if args[1] == "create":
            name = args[args.index("--name") + 1]
            self.created[name] = list(args)
            if self.on_create is not None:
                self.on_create(name)
            return 0
        if args[1] == "exec":
            return self.pg_ready
        if args[1] == "start" and args[2] != "--attach":
            self.started.add(args[2])
            return 0 if args[2] in self.created else 1
        if command != ["start", "--attach"] or args[3] not in self.created:
            return 1
        name = args[3]
        for part, error in self.failures.items():
            if part in name:
                raise error
        created = self.created.pop(name)
        volume = mounts(created)["/deps"][1]
        if created[-3:] == ["test", "-f", MARKER]:
            return 0 if self.volumes.get(volume) else 1
        if created[-1] == "install":
            log.write(b"install output\n")
            if self.on_install is not None:
                self.on_install(created)
            self.volumes[volume] = self.install_exit == 0 and self.marker
            return self.install_exit
        log.write(b"230 passed in 1.00s\n")
        if self.on_test is not None:
            self.on_test(name)
        return self.test_exit


@pytest.fixture
def setup(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    docker = FakeDocker()
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=docker)
    [task_id] = office.start_tasks(app, me["id"])
    src = tmp_path / "chat"
    (src / "api").mkdir(parents=True)
    (src / "contracts").mkdir()
    (src / "api" / "pyproject.toml").write_text(PYPROJECT)
    (src / "api" / "uv.lock").write_text(LOCK)
    (src / "contracts" / "api.md").write_text("# API\n")
    (src / ".env.example").write_text("DEMO_ACCESS_CODE=\n")
    return app, docker, sandbox, PythonSandbox(sandbox), task_id, src


def volume_name(pyproject=PYPROJECT, lock=LOCK):
    digest = hashlib.sha256()
    for name, text in (("pyproject.toml", pyproject), ("uv.lock", lock)):
        data = text.encode()
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return "office-py-" + digest.hexdigest()[:16]


def assert_sandboxed(args):
    assert option(args, "--user") == ["10001:10001"]
    assert option(args, "--cap-drop") == ["ALL"]
    assert option(args, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in args
    assert option(args, "--tmpfs") == ["/tmp:rw,size=1g", "/work:rw,uid=10001,gid=10001,mode=0755,size=1g"]
    assert option(args, "--memory") == ["3g"]
    assert option(args, "--cpus") == ["2"]
    assert option(args, "--pids-limit") == ["1024"]


def test_install_and_tests_run_in_isolated_containers(setup, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "office-secret-value")
    app, docker, sandbox, python, task_id, src = setup
    seen = {}

    def inspect(args):
        files = Path(mounts(args)["/src"][1])
        seen.update(files=sorted(os.listdir(files)), content=(files / "uv.lock").read_text())

    docker.on_install = inspect

    checks = python.run_api_tests(task_id, src, {"commit_sha": "c" * 40})

    assert [check.command for check in checks] == [
        "uv sync --locked --no-install-project", "python -m pytest -p no:cacheprovider"
    ]
    assert [check.exit_code for check in checks] == [0, 0]
    assert [check.artifact_urls for check in checks] == [
        [f"/api/tasks/{task_id}/artifacts/api-install-1.log"], [f"/api/tasks/{task_id}/artifacts/api-test-1.log"]
    ]
    assert seen == {"files": ["pyproject.toml", "uv.lock"], "content": LOCK}

    [install] = docker.step_args("api-install")
    assert_sandboxed(install)
    assert option(install, "--label") == [sandbox.label]
    assert "--network" not in install
    install_mounts = mounts(install)
    assert set(install_mounts) == {"/src", "/deps"}
    assert install_mounts["/src"][0] == "bind" and install_mounts["/src"][2]
    assert install_mounts["/src"][1] != str(src)
    assert not os.path.exists(install_mounts["/src"][1])
    assert install_mounts["/deps"] == ("volume", volume_name(), False)
    assert install[-3:] == ["office-python-sandbox:p5", "sandbox-python-step", "install"]

    [probe] = docker.step_args("probe")
    assert docker.calls.index(install) < docker.calls.index(probe)
    assert_sandboxed(probe)
    assert option(probe, "--network") == ["none"]
    assert mounts(probe) == {"/deps": ("volume", volume_name(), True)}
    assert probe[-4:] == ["office-python-sandbox:p5", "test", "-f", MARKER]

    [network_call] = [args for args in docker.calls if args[1:3] == ["network", "create"]]
    network = network_call[-1]
    assert network_call == ["docker", "network", "create", "--internal", "--label", sandbox.label, network]
    assert network.startswith(f"office-pynet-{task_id}-")

    [test] = docker.step_args("api-test")
    assert_sandboxed(test)
    assert option(test, "--label") == [sandbox.label]
    assert option(test, "--network") == [network]
    assert "TEST_DATABASE_URL=postgresql://vibe:vibe@postgres:5432/vibe_test" in option(test, "--env")
    assert "PYTHONDONTWRITEBYTECODE=1" in option(test, "--env")
    assert mounts(test) == {"/src": ("bind", str(src), True), "/deps": ("volume", volume_name(), True)}
    assert test[-3:] == ["office-python-sandbox:p5", "sandbox-python-step", "test"]

    [postgres] = docker.create_args("pg")
    name = postgres[postgres.index("--name") + 1]
    assert name.startswith(f"office-{task_id}-pg-")
    assert option(postgres, "--label") == [sandbox.label]
    assert option(postgres, "--network") == [network]
    assert option(postgres, "--network-alias") == ["postgres"]
    assert option(postgres, "--user") == ["70:70"]
    assert option(postgres, "--cap-drop") == ["ALL"]
    assert option(postgres, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in postgres
    assert option(postgres, "--memory") == ["512m"]
    assert option(postgres, "--pids-limit") == ["256"]
    assert postgres[-1] == "postgres:16-alpine"
    assert ["docker", "start", name] in docker.calls

    for args in docker.calls:
        text = " ".join(args)
        assert "office-secret-value" not in text
        assert "docker.sock" not in text
        assert "--privileged" not in args
    assert docker.created == {}
    assert docker.networks == set()

    log = app.state.settings.artifacts_dir / task_id / "api-test-1.log"
    assert log.read_text() == "230 passed in 1.00s\n"
    events = [event for event in app.state.store.events_after(task_id, 0, 1000) if event.type == "check_result"]
    assert [(event.payload["step"], event.tool, event.artifact) for event in events] == [
        ("api-install", "sandbox", "api-install-1.log"), ("api-test", "sandbox", "api-test-1.log")
    ]
    assert all(event.payload["commit_sha"] == "c" * 40 for event in events)


def test_ready_volume_is_reused_without_install(setup):
    _, docker, _, python, task_id, src = setup
    docker.volumes[volume_name()] = True
    checks = python.run_api_tests(task_id, src)
    assert [check.command for check in checks] == ["python -m pytest -p no:cacheprovider"]
    assert docker.step_args("api-install") == []
    assert len(docker.step_args("probe")) == 1


def test_changed_lock_file_uses_new_volume(setup):
    _, docker, _, python, task_id, src = setup
    python.run_api_tests(task_id, src)
    (src / "api" / "uv.lock").write_text("version = 2\n")
    python.run_api_tests(task_id, src)
    volumes = [mounts(args)["/deps"][1] for args in docker.step_args("api-install")]
    assert volumes == [volume_name(), volume_name(lock="version = 2\n")]


def test_changed_pyproject_with_same_lock_installs_again(setup):
    _, docker, _, python, task_id, src = setup
    python.run_api_tests(task_id, src)
    changed = PYPROJECT + 'dependencies = ["requests"]\n'
    (src / "api" / "pyproject.toml").write_text(changed)
    docker.install_exit = 1
    checks = python.run_api_tests(task_id, src)
    assert [(check.command, check.exit_code) for check in checks] == [("uv sync --locked --no-install-project", 1)]
    volumes = [mounts(args)["/deps"][1] for args in docker.step_args("api-install")]
    assert volumes == [volume_name(), volume_name(pyproject=changed)]
    assert len(docker.step_args("api-test")) == 1


def test_failed_install_removes_volume_and_skips_tests(setup):
    _, docker, _, python, task_id, src = setup
    docker.install_exit = 1
    checks = python.run_api_tests(task_id, src)
    assert [(check.command, check.exit_code) for check in checks] == [("uv sync --locked --no-install-project", 1)]
    assert volume_name() not in docker.volumes
    assert docker.calls[-1] == ["docker", "volume", "rm", "-f", volume_name()]
    assert docker.step_args("api-test") == []
    assert not any(args[1:3] == ["network", "create"] for args in docker.calls)


def test_unconfirmed_install_removes_volume(setup):
    _, docker, _, python, task_id, src = setup
    docker.marker = False
    with pytest.raises(SandboxError, match="готовность тома"):
        python.run_api_tests(task_id, src)
    assert volume_name() not in docker.volumes
    assert docker.step_args("api-test") == []


def test_runner_error_during_install_removes_container_before_volume(setup):
    _, docker, _, python, task_id, src = setup
    docker.failures["api-install"] = RuntimeError("docker died")
    with pytest.raises(RuntimeError):
        python.run_api_tests(task_id, src)
    assert docker.calls[-2:] == [
        ["docker", "rm", "-f", f"office-{task_id}-api-install-1"], ["docker", "volume", "rm", "-f", volume_name()]
    ]
    assert docker.created == {}


def cleanup_calls(docker, task_id):
    [postgres] = docker.create_args("pg")
    pg = postgres[postgres.index("--name") + 1]
    [network] = [args[-1] for args in docker.calls if args[1:3] == ["network", "create"]]
    return (
        docker.calls.index(["docker", "rm", "-f", pg]),
        docker.calls.index(["docker", "network", "rm", network]),
    )


def test_failed_tests_remove_network_and_postgres(setup):
    _, docker, _, python, task_id, src = setup
    docker.test_exit = 1
    checks = python.run_api_tests(task_id, src)
    assert checks[-1].exit_code == 1
    pg, network = cleanup_calls(docker, task_id)
    assert pg < network
    assert docker.created == {} and docker.networks == set()


def test_postgres_start_timeout_removes_network_and_postgres(setup, monkeypatch):
    _, docker, _, python, task_id, src = setup
    monkeypatch.setattr(python_sandbox, "PG_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(python_sandbox, "PG_POLL_SECONDS", 0.01)
    docker.pg_ready = 2
    with pytest.raises(SandboxError, match="PostgreSQL"):
        python.run_api_tests(task_id, src)
    assert docker.step_args("api-test") == []
    cleanup_calls(docker, task_id)
    assert docker.created == {} and docker.networks == set()


def test_cancel_during_tests_removes_network_and_postgres(setup):
    _, docker, sandbox, python, task_id, src = setup
    docker.on_test = lambda name: sandbox.cancel_task(task_id)
    with pytest.raises(SandboxCancelled):
        python.run_api_tests(task_id, src)
    pg, network = cleanup_calls(docker, task_id)
    assert docker.calls.index(["docker", "rm", "-f", f"office-{task_id}-api-test-1"]) < pg < network
    assert docker.created == {} and docker.networks == set()
    assert not sandbox.has_containers(task_id)


def test_runner_error_during_tests_removes_test_container_first(setup):
    _, docker, _, python, task_id, src = setup
    docker.failures["api-test"] = RuntimeError("docker died")
    with pytest.raises(RuntimeError):
        python.run_api_tests(task_id, src)
    pg, network = cleanup_calls(docker, task_id)
    assert docker.calls[-3:] == [docker.calls[pg - 1], docker.calls[pg], docker.calls[network]]
    assert docker.calls[pg - 1] == ["docker", "rm", "-f", f"office-{task_id}-api-test-1"]
    assert docker.created == {} and docker.networks == set()


@pytest.mark.parametrize("rel", [".env", "api/.env", "api/.env.local", ".env.production"])
def test_environment_files_are_refused(setup, rel):
    _, docker, _, python, task_id, src = setup
    (src / rel).write_text("SECRET=1\n")
    with pytest.raises(SandboxError, match="файлы окружения"):
        python.run_api_tests(task_id, src)
    assert not any(args[:2] == ["docker", "create"] for args in docker.calls)


def test_symlinked_lock_file_is_refused(setup, tmp_path):
    _, docker, _, python, task_id, src = setup
    outside = tmp_path / "outside.lock"
    outside.write_text(LOCK)
    (src / "api" / "uv.lock").unlink()
    (src / "api" / "uv.lock").symlink_to(outside)
    with pytest.raises(SandboxError, match="uv.lock"):
        python.run_api_tests(task_id, src)
    assert not any(args[:2] == ["docker", "create"] for args in docker.calls)


def test_missing_lock_file_is_refused(setup):
    _, docker, _, python, task_id, src = setup
    (src / "api" / "uv.lock").unlink()
    with pytest.raises(SandboxError, match="uv.lock"):
        python.run_api_tests(task_id, src)
    assert not any(args[:2] == ["docker", "create"] for args in docker.calls)


@pytest.mark.parametrize("image", ["office-python-sandbox:p5", "postgres:16-alpine"])
def test_missing_image_is_refused(setup, image):
    _, docker, _, python, task_id, src = setup
    docker.images.discard(image)
    with pytest.raises(SandboxError, match="docker build -t office-python-sandbox:p5"):
        python.run_api_tests(task_id, src)
    assert not any(args[:2] == ["docker", "create"] for args in docker.calls)


def test_stale_containers_and_networks_are_removed(setup):
    _, docker, sandbox, python, _, _ = setup
    docker.created = {"office-old-pg-1": ["--label", sandbox.label], "foreign": ["--label", "other"]}
    docker.networks = {"office-pynet-old-1"}
    python.remove_stale()
    assert ["docker", "rm", "-f", "office-old-pg-1"] in docker.calls
    assert ["docker", "network", "rm", "office-pynet-old-1"] in docker.calls
    assert list(docker.created) == ["foreign"]
    assert docker.networks == set()
    assert ["docker", "network", "ls", "-q", "--filter", f"label={sandbox.label}"] in docker.calls
