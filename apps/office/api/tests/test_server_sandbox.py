from pathlib import Path

import pytest
from test_python_sandbox import FakeDocker, mounts, option, setup  # noqa: F401

from app.python_sandbox import APPLY_SCHEMA, PythonSandbox
from app.sandbox import COMPAT_COMMANDS, Sandbox, SandboxError, StepDenied, parse_junit, suite_steps
from app.schemas import AgentRole

TEST_FILE = "apps/chat/api/tests/office/test_tsk_1.py"


def junit(cases: str) -> bytes:
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'.encode()


@pytest.mark.parametrize(
    ("cases", "status", "line"),
    [
        ('<testcase classname="tests.office.test_tsk_1" name="test_a"/>', "passed", None),
        (
            '<testcase classname="tests.office.test_tsk_1" name="test_a"><failure message="assert 1 == 2">'
            "def test_a():\n&gt;       assert 1 == 2\nE       assert 1 == 2\n\ntests/office/test_tsk_1.py:4: AssertionError"
            "</failure></testcase>",
            "failed",
            4,
        ),
        (
            '<testcase classname="tests.office.test_tsk_1" name="test_a"><failure message="KeyError: \'x\'">'
            "tests/office/test_tsk_1.py:7: KeyError</failure></testcase>",
            "test_error",
            7,
        ),
        (
            '<testcase classname="" name="tests.office.test_tsk_1"><error message="collection failure">'
            "ImportError: cannot import name</error></testcase>",
            "test_error",
            None,
        ),
        ('<testcase classname="tests.test_runs" name="test_other"/>', "no_evidence", None),
        (
            '<testcase classname="tests.office.test_tsk_1" name="test_a"><skipped message="skip"/></testcase>',
            "no_evidence",
            None,
        ),
    ],
)
def test_pytest_report_is_classified(cases, status, line):
    result = parse_junit(junit(cases), TEST_FILE)
    assert (result.status, result.line) == (status, line)


def test_missing_or_broken_pytest_report_is_no_evidence():
    assert parse_junit(None, TEST_FILE).status == "no_evidence"
    assert parse_junit(b"<testsuites", TEST_FILE).status == "no_evidence"


@pytest.mark.parametrize(
    ("changed", "change_class", "playwright", "expected"),
    [
        (["apps/chat/web/src/App.tsx"], "frontend_ui", True, ["lint", "test", "build", "e2e"]),
        (["apps/chat/web/src/App.tsx"], "backend_api", False, ["lint", "test", "build", "api-test"]),
        (["apps/chat/contracts/api.md"], None, False, ["lint", "test", "build", "api-test"]),
        (["apps/chat/web/src/App.tsx", "apps/chat/api/app/main.py"], "frontend_ui", True,
         ["lint", "test", "build", "e2e", "api-test"]),
        (["apps/chat/api/app/schema.sql"], None, False, ["lint", "test", "build", "api-test", "api-compat"]),
    ],
)
def test_suite_steps_follow_diff_and_class(changed, change_class, playwright, expected):
    assert suite_steps(changed, change_class, playwright) == expected


def test_api_compat_uses_one_database_for_three_steps(setup, tmp_path):
    app, docker, sandbox, python, task_id, src = setup
    task_src = tmp_path / "task-chat"
    (task_src / "api" / "app").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        (task_src / "api" / name).write_text((src / "api" / name).read_text())
    checks = python.api_compat(task_id, src, task_src, {"commit_sha": "c" * 40})
    assert [check.command for check in checks] == ["uv sync --locked --no-install-project", *COMPAT_COMMANDS]
    assert [check.exit_code for check in checks] == [0, 0, 0, 0]
    runs = docker.step_args("api-compat")
    assert len(runs) == 3
    assert [mounts(args)["/src"][1] for args in runs] == [str(src), str(task_src), str(src)]
    assert len({option(args, "--network")[-1] for args in runs}) == 1
    assert len(docker.create_args("pg")) == 1
    assert runs[1][-4:] == ["/deps/venv/bin/python", "-c", APPLY_SCHEMA, "/src/api/app/schema.sql"]
    assert runs[0][-2:] == ["sandbox-python-step", "test"]
    assert all(mounts(args)["/deps"][2] for args in runs)
    assert docker.created == {} and docker.networks == set()


def test_api_compat_stops_at_first_failure(setup, tmp_path):
    _, docker, _, python, task_id, src = setup
    docker.test_exit = 1
    checks = python.api_compat(task_id, src, src)
    assert [check.command for check in checks][-1] == COMPAT_COMMANDS[0]
    assert len(docker.step_args("api-compat")) == 1


def test_pytest_junit_report_is_kept_as_artifact(setup):
    app, docker, sandbox, python, task_id, src = setup

    outs = {}

    def remember(name):
        outs[name] = mounts(docker.created[name]).get("/out")

    def write_report(name):
        out = Path(outs[name][1])
        (out / "junit.xml").write_bytes(junit('<testcase classname="tests.office.test_tsk_1" name="test_a"/>'))

    docker.on_create = remember
    docker.on_test = write_report
    checks = python.run_api_tests(task_id, src, junit=True)
    test = checks[-1]
    assert test.artifact_urls[-1].endswith("api-test-1-junit.xml")
    [args] = docker.step_args("api-test")
    assert args[-1] == "--junitxml=/out/junit.xml"
    assert mounts(args)["/out"][2] is False
    report = (app.state.settings.artifacts_dir / task_id / "api-test-1-junit.xml").read_bytes()
    assert parse_junit(report, TEST_FILE).status == "passed"


class LockRunner:
    def __init__(self, result: bytes | None, exit_code: int = 0):
        self.result = result
        self.exit_code = exit_code
        self.calls = []
        self.created = {}

    def __call__(self, args, timeout, log):
        self.calls.append(list(args))
        if args[1:3] == ["image", "inspect"]:
            return 0
        if args[1] == "create":
            self.created[args[args.index("--name") + 1]] = list(args)
            return 0
        if args[1:3] == ["start", "--attach"]:
            created = self.created.pop(args[3])
            seen = mounts(created)
            if self.result is not None:
                name = "package-lock.json" if "sandbox-step" in created else "uv.lock"
                (Path(seen["/out"][1]) / name).write_bytes(self.result)
            log.write(b"lock output\n")
            return self.exit_code
        return 0


def test_node_lock_step_sees_only_manifest_and_lock(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    runner = LockRunner(b'{"lockfileVersion": 3, "new": true}\n')
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=runner)
    root = app.state.workspace.worktree_path(task_id)
    result = sandbox.lock_node(task_id, root)
    assert result.exit_code == 0 and result.command.startswith("npm install --package-lock-only --ignore-scripts")
    [args] = [args for args in runner.calls if args[1] == "create"]
    seen = mounts(args)
    assert set(seen) == {"/src", "/out"}
    assert seen["/src"][2] is True and seen["/out"][2] is False
    assert "--network" not in args
    assert args[-3:-1] == ["sandbox-step", "lock"]
    assert (root / "apps/chat/web/package-lock.json").read_bytes() == b'{"lockfileVersion": 3, "new": true}\n'


def test_failed_node_lock_keeps_lock_file(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    runner = LockRunner(b"broken", exit_code=1)
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=runner)
    root = app.state.workspace.worktree_path(task_id)
    before = (root / "apps/chat/web/package-lock.json").read_bytes()
    assert sandbox.lock_node(task_id, root).exit_code == 1
    assert (root / "apps/chat/web/package-lock.json").read_bytes() == before


def test_python_lock_step_copies_uv_lock(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    runner = LockRunner(b"version = 2\n")
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=runner)
    root = app.state.workspace.worktree_path(task_id)
    result = PythonSandbox(sandbox).lock(task_id, root / "apps/chat")
    assert result.exit_code == 0 and result.command == "uv lock"
    [args] = [args for args in runner.calls if args[1] == "create"]
    assert set(mounts(args)) == {"/src", "/out"} and args[-2:] == ["sandbox-python-step", "lock"]
    assert (root / "apps/chat/api/uv.lock").read_bytes() == b"version = 2\n"


def test_oversized_lock_result_is_refused(office, origin, tmp_path):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=LockRunner(b"x" * (2 * 1024 * 1024 + 1)))
    with pytest.raises(SandboxError, match="больше допустимого"):
        sandbox.lock_node(task_id, app.state.workspace.worktree_path(task_id))


@pytest.mark.parametrize(("change_class", "role"), [("frontend_ui", AgentRole.developer), ("backend_api", AgentRole.qa)])
def test_api_test_step_is_only_for_backend_developer(office, origin, tmp_path, change_class, role):
    app = office.workspace_app(origin, tmp_path)
    _, me = office.login(app=app)
    [task_id] = office.start_tasks(app, me["id"])
    office.set_task(task_id, change_class=change_class)
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace, runner=FakeDocker())
    with pytest.raises(StepDenied):
        sandbox.run_step(task_id, "api-test", role)
    with pytest.raises(StepDenied):
        sandbox.run_step(task_id, "lock", None)
