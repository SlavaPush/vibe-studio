import pytest

from app.config import POLICIES_DIR
from app.paths import PathDenied, PathPolicy, glob_regex, normalize, resolve_inside
from app.schemas import AgentRole

POLICY = PathPolicy(POLICIES_DIR / "paths.yaml")


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("api/**", "api", True),
        ("api/**", "api/app/main.py", True),
        ("api/**", "apix/main.py", False),
        ("**/.env*", ".env", True),
        ("**/.env*", "web/src/.env.local", True),
        ("**/*.test.*", "web/src/App.test.tsx", True),
        ("web/e2e/office/**/*.pw.ts", "web/e2e/office/a.pw.ts", True),
        ("web/e2e/office/**/*.pw.ts", "web/e2e/office/sub/a.pw.ts", True),
        ("web/e2e/office/**/*.pw.ts", "web/e2e/a.pw.ts", False),
        ("web/tsconfig*.json", "web/tsconfig.app.json", True),
        ("web/src/**", "web/src/a/b.tsx", True),
        ("web/src/**", "web/srcx/b.tsx", False),
    ],
)
def test_glob_regex(pattern, path, matches):
    assert bool(glob_regex(pattern).fullmatch(path)) is matches


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../x", "web/../api/x", "./web", "web//src", "web/src/", "web\\src", "a\0b", "x" * 401]
)
def test_normalize_rejects(path):
    with pytest.raises(PathDenied):
        normalize(path)


@pytest.mark.parametrize(
    "path",
    [
        "apps/chat/API/main.py",
        "apps/chat/Api/x.ts",
        "apps/chat/web/SRC/test/setup.ts",
        "apps/chat/web/src/App.TEST.tsx",
        "apps/chat/web/src/.ENV",
        "apps/chat/WEB/package.json",
        "apps/chat/web/Package-Lock.json",
        "apps/chat/web/VITE.config.ts",
        "apps/chat/web/src/API/client.ts",
        "Apps/Office/policies/paths.yaml",
    ],
)
def test_case_variants_are_denied(path):
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.developer, path, 10)


@pytest.mark.parametrize(
    ("role", "path"),
    [
        (AgentRole.developer, "apps/chat/web/src/a/B.tsx"),
        (AgentRole.developer, "apps/chat/web/src/components/chat/Copy.tsx"),
        (AgentRole.developer, "apps/chat/web/public/icon.svg"),
        (AgentRole.developer, "apps/chat/web/index.html"),
        (AgentRole.qa, "apps/chat/web/e2e/office/a.pw.ts"),
        (AgentRole.qa, "apps/chat/web/e2e/office/sub/a.pw.ts"),
    ],
)
def test_monorepo_paths_allowed(role, path):
    assert POLICY.check_write(role, path, 10) == path
    assert POLICY.check_any_writer(path, 10) == path


@pytest.mark.parametrize(
    "path",
    [
        "apps/office/policies/paths.yaml",
        "apps/office/api/app/workspace.py",
        "apps/office/web/src/App.tsx",
        ".github/workflows/ci.yml",
        ".github/workflows/office-paths.yml",
        "apps/chat/web/src/api/client.ts",
        "apps/chat/web/src/mocks/contract-examples.ts",
        "apps/chat/web/src/test/setup.ts",
        "apps/chat/web/src/App.test.tsx",
        "apps/chat/api/app/main.py",
        "apps/chat/contracts/api.md",
        "apps/chat/infra/Dockerfile",
        "apps/chat/web/package.json",
        "apps/chat/web/package-lock.json",
        "apps/chat/web/vite.config.ts",
        "apps/chat/web/e2e/office/a.ts",
        "apps/chat/web/e2e/office/support/mock-api.ts",
        "apps/chat/web/e2e/office/support/mock-api.test.ts",
        "apps/chat/web/e2e/smoke.pw.ts",
        "apps/chat/web/.env.local",
        "web/src/App.tsx",
        "README.md",
        "CODEOWNERS",
        "package.json",
    ],
)
def test_monorepo_paths_denied(path):
    for role in (AgentRole.developer, AgentRole.qa):
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10)
    with pytest.raises(PathDenied):
        POLICY.check_any_writer(path, 10)


def test_role_rules():
    for role, path in [
        (AgentRole.qa, "apps/chat/web/src/a.ts"),
        (AgentRole.developer, "apps/chat/web/e2e/office/a.pw.ts"),
        (AgentRole.critic, "apps/chat/web/src/a.ts"),
        (AgentRole.developer, "apps/chat/web/src/a.sh"),
    ]:
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10)
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.developer, "apps/chat/web/src/a.ts", POLICY.max_file_bytes + 1)


def test_symlink_component_is_rejected(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(PathDenied):
        resolve_inside(tmp_path, "link/file.ts")
    assert resolve_inside(tmp_path, "real/new/file.ts") == tmp_path / "real" / "new" / "file.ts"
