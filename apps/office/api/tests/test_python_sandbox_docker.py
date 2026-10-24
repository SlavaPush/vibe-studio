import io
import os
import re
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest
from conftest import Office, Origin

from app.python_sandbox import PythonSandbox
from app.sandbox import Sandbox

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
