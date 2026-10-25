import io
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest
from conftest import Office, Origin

from app.python_sandbox import PythonSandbox
from app.sandbox import COMPAT_COMMANDS, Sandbox

REPO = Path(__file__).resolve().parents[4]
PROBE = """
import ssl
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("https://pypi.org/simple/", timeout=10)
except urllib.error.HTTPError:
    sys.exit(1)
except urllib.error.URLError as error:
    reason = error.reason
    if isinstance(reason, OSError) and not isinstance(reason, ssl.SSLError):
        print("NETWORK_BLOCKED", type(reason).__name__)
        sys.exit(42)
    sys.exit(1)
except OSError:
    sys.exit(1)
sys.exit(0)
"""

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not os.environ.get("OFFICE_TEST_DOCKER"), reason="OFFICE_TEST_DOCKER не задан"),
    pytest.mark.skipif(
        not os.environ.get("OFFICE_TEST_PYTHON_SANDBOX"), reason="OFFICE_TEST_PYTHON_SANDBOX не задан"
    ),
]


def docker_lines(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return result.stdout.split()


@pytest.fixture
def real(tmp_path):
    office = Office(f"sqlite:///{tmp_path / 'office.sqlite'}", tmp_path)
    app = office.workspace_app(Origin(tmp_path), tmp_path)
    _, me = office.login(app=app)
    sandbox = Sandbox(app.state.settings, app.state.store, app.state.workspace)
    prefix = f"office-py-test-{uuid.uuid4().hex[:8]}-"
    python = PythonSandbox(sandbox, volume_prefix=prefix)
    [task_id] = office.start_tasks(app, me["id"])
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "apps/chat"], cwd=REPO, capture_output=True, check=True
    )
    export = tmp_path / "export"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(export, filter="data")
    yield app, sandbox, python, task_id, export / "apps" / "chat"
    python.remove_stale()
    office.close()
    for volume in docker_lines("volume", "ls", "-q", "--filter", f"name={prefix}"):
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)


def test_chat_api_tests_pass_without_network(real):
    app, sandbox, python, task_id, src = real
    assert not (src / ".env").exists()
    task_dir = app.state.settings.artifacts_dir / task_id

    checks = python.run_api_tests(task_id, src)
    logs = "\n".join(
        (task_dir / url.rsplit("/", 1)[1]).read_text(errors="replace")[-5000:]
        for check in checks
        for url in check.artifact_urls
    )
    assert [check.command.split()[:2] for check in checks] == [["uv", "sync"], ["python", "-m"]], logs
    assert [check.exit_code for check in checks] == [0, 0], logs
    summary = (task_dir / "api-test-1.log").read_text(errors="replace").strip().splitlines()[-1]
    passed = re.search(r"(\d+) passed", summary)
    assert passed and int(passed.group(1)) >= 230, summary
    assert not re.search(r"\d+ (failed|errors?)\b", summary), summary
    print(summary)

    [probe] = python._run_api_tests(task_id, src, command=["/deps/venv/bin/python", "-c", PROBE])
    probe_log = (task_dir / probe.artifact_urls[0].rsplit("/", 1)[1]).read_text(errors="replace")
    assert probe.exit_code == 42, probe_log
    assert "NETWORK_BLOCKED" in probe_log
    print(probe_log.strip(), "exit", probe.exit_code)

    assert docker_lines("ps", "-aq", "--filter", f"label={sandbox.label}") == []
    assert docker_lines("network", "ls", "-q", "--filter", f"label={sandbox.label}") == []


def task_copy(src: Path, target: Path, tail: str) -> Path:
    shutil.copytree(src, target)
    schema = target / "api" / "app" / "schema.sql"
    schema.write_text(schema.read_text(encoding="utf-8") + tail, encoding="utf-8")
    return target


def test_api_compat_accepts_additive_schema(real, tmp_path):
    app, sandbox, python, task_id, src = real
    task_src = task_copy(src, tmp_path / "task-chat", "\nALTER TABLE users ADD COLUMN IF NOT EXISTS nickname text;\n")
    checks = python.api_compat(task_id, src, task_src)
    task_dir = app.state.settings.artifacts_dir / task_id
    logs = "\n".join((task_dir / check.artifact_urls[0].rsplit("/", 1)[1]).read_text(errors="replace")[-3000:] for check in checks)
    assert [check.command for check in checks][-3:] == list(COMPAT_COMMANDS), logs
    assert all(check.exit_code == 0 for check in checks), logs
    assert docker_lines("network", "ls", "-q", "--filter", f"label={sandbox.label}") == []


def test_api_compat_rejects_schema_that_breaks_base_version(real, tmp_path):
    app, sandbox, python, task_id, src = real
    breaking = "\nALTER TABLE users ADD COLUMN IF NOT EXISTS must text DEFAULT 'x';\nALTER TABLE users ALTER COLUMN must DROP DEFAULT;\nALTER TABLE users ALTER COLUMN must SET NOT NULL;\n"
    checks = python.api_compat(task_id, src, task_copy(src, tmp_path / "task-chat", breaking))
    assert [check.command for check in checks][-3:] == list(COMPAT_COMMANDS)
    assert [check.exit_code for check in checks][-3:-1] == [0, 0]
    assert checks[-1].exit_code != 0


def test_real_uv_lock_adds_dependency(real):
    app, sandbox, python, task_id, src = real
    pyproject = src / "api" / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('"httpx>=0.28.1",', '"httpx>=0.28.1",\n    "idna>=3.0",'),
        encoding="utf-8",
    )
    result = python.lock(task_id, src)
    log = (app.state.settings.artifacts_dir / task_id / result.artifact_urls[0].rsplit("/", 1)[1]).read_text(errors="replace")
    assert result.exit_code == 0, log
    assert 'name = "idna"' in (src / "api" / "uv.lock").read_text(encoding="utf-8")
    assert '{ name = "idna", specifier = ">=3.0" }' in (src / "api" / "uv.lock").read_text(encoding="utf-8")
