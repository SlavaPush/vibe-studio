import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import Origin, git

API_DIR = Path(__file__).resolve().parents[1]
SCHEMA = b"CREATE TABLE IF NOT EXISTS users (\n    id uuid PRIMARY KEY,\n    source_hash text NOT NULL\n);\n"


@pytest.fixture
def repo(tmp_path):
    return Origin(tmp_path).work


def branch(
    repo: Path, files: dict[str, bytes | None], start: dict[str, bytes] | None = None
) -> tuple[str, str]:
    for rel, content in (start or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(content)
        git(repo, "add", rel)
    if start:
        git(repo, "commit", "-q", "--no-verify", "-m", "start")
    base = git(repo, "rev-parse", "main")
    git(repo, "switch", "-q", "-c", f"office/tsk_{len(files)}{os.urandom(3).hex()}", base)
    for rel, content in files.items():
        if content is None:
            git(repo, "rm", "-q", rel)
            continue
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        git(repo, "add", rel)
    git(repo, "commit", "-q", "--no-verify", "-m", "task")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    return base, head


def check(repo: Path, base: str, head: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_branch", "--repo", str(repo), "--base", base, "--head", head],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_interface_only_branch_passes(repo):
    base, head = branch(
        repo,
        {
            "apps/chat/web/src/App.tsx": b"export function App() {\n  return 'copy'\n}\n",
            "apps/chat/web/src/components/Copy.tsx": b"export const copy = 1\n",
            "apps/chat/web/e2e/office/tsk_1.pw.ts": b"import { test } from '@playwright/test'\n",
        },
    )
    result = check(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "нарушений нет" in result.stdout


def test_diff_starts_at_merge_base(repo):
    base, head = branch(repo, {"apps/chat/web/src/Copy.tsx": b"export const copy = 1\n"})
    (repo / "apps/office/policies/paths.yaml").write_text("deny: [changed]\n")
    git(repo, "commit", "-q", "-am", "office")
    result = check(repo, git(repo, "rev-parse", "main"), head)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"apps/office/policies/paths.yaml": b"deny: []\nroles: {}\n"}, "apps/office/policies/paths.yaml: "),
        ({".github/workflows/ci.yml": b"name: changed\n"}, ".github/workflows/ci.yml: "),
        ({".github/workflows/office-paths.yml": b"name: new\n"}, ".github/workflows/office-paths.yml: "),
        ({"apps/chat/api/main.py": b"app = 1\n"}, "apps/chat/api/main.py: "),
        ({"apps/chat/api/tests/test_runs.py": b"def test_x():\n    pass\n"}, "apps/chat/api/tests/test_runs.py: "),
        ({"apps/chat/infra/Dockerfile": b"FROM scratch\n"}, "apps/chat/infra/Dockerfile: "),
        ({"apps/chat/api/.env": b"KEY=1\n"}, "apps/chat/api/.env: "),
        ({"apps/chat/web/package.json": b'{"name": "web", "scripts": {"test": "true"}}\n'}, "вне разделов зависимостей"),
        (
            {"apps/chat/api/app/schema.sql": SCHEMA + b"ALTER TABLE users DROP COLUMN source_hash;\n"},
            "неаддитивное изменение схемы",
        ),
        ({"apps/chat/web/src/App.test.tsx": None}, "apps/chat/web/src/App.test.tsx: "),
        ({"apps/chat/web/src/Quiet.tsx": b"// eslint-disable-next-line\nexport {}\n"}, "eslint-disable"),
        ({"apps/chat/web/src/Bin.ts": b"// \x00\n// @ts-nocheck\nexport {}\n"}, "@ts-nocheck"),
    ],
)
def test_forbidden_branch_fails(repo, files, expected):
    base, head = branch(repo, {"apps/chat/web/src/Fine.tsx": b"export {}\n", **files})
    result = check(repo, base, head)
    assert result.returncode == 1
    assert expected in result.stdout
    assert "Fine.tsx" not in result.stdout


def test_server_zone_branch_passes(repo):
    base, head = branch(
        repo,
        {
            "apps/chat/api/app/main.py": b"app = 1\n",
            "apps/chat/api/app/schema.sql": SCHEMA + b"ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname text;\n",
            "apps/chat/contracts/api.md": b"# API\n\nnew field\n",
            "apps/chat/web/src/api/client.ts": b"export {}\n",
            "apps/chat/web/package.json": b'{"name": "web", "dependencies": {"left-pad": "^1.3.0"}}\n',
            "apps/chat/web/package-lock.json": b'{"name": "web", "lockfileVersion": 3, "packages": {}}\n' + b" " * 300000,
            "apps/chat/api/tests/office/test_tsk_1.py": b"def test_new():\n    assert True\n",
        },
        start={"apps/chat/api/app/schema.sql": SCHEMA},
    )
    result = check(repo, base, head)
    assert result.returncode == 0, result.stdout + result.stderr


def test_binary_content_is_reported(repo):
    base, head = branch(repo, {"apps/chat/web/src/Bin.ts": b"export {}\n\x00\n"})
    result = check(repo, base, head)
    assert result.returncode == 1
    assert "apps/chat/web/src/Bin.ts: двоичное содержимое запрещено." in result.stdout


@pytest.mark.parametrize("value", ["main", "HEAD~1", "a" * 39])
def test_only_full_sha_is_accepted(repo, value):
    head = git(repo, "rev-parse", "main")
    result = check(repo, value, head)
    assert result.returncode == 1
    assert "полными SHA" in result.stderr
