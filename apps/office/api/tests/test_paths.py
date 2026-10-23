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
        "API/main.py",
        "Api/x.ts",
        "web/SRC/test/setup.ts",
        "web/src/App.TEST.tsx",
        "web/src/.ENV",
        "WEB/package.json",
        "web/Package-Lock.json",
        "web/VITE.config.ts",
    ],
)
def test_case_variants_are_denied(path):
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.developer, path, 10)


def test_role_rules():
    assert POLICY.check_write(AgentRole.developer, "web/src/a/B.tsx", 10) == "web/src/a/B.tsx"
    assert POLICY.check_write(AgentRole.qa, "web/e2e/office/a.pw.ts", 10) == "web/e2e/office/a.pw.ts"
    for role, path in [
        (AgentRole.qa, "web/src/a.ts"),
        (AgentRole.developer, "web/e2e/office/a.pw.ts"),
        (AgentRole.critic, "web/src/a.ts"),
        (AgentRole.developer, "web/src/a.sh"),
    ]:
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10)
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.developer, "web/src/a.ts", POLICY.max_file_bytes + 1)
    assert POLICY.check_any_writer("web/e2e/office/a.pw.ts") == "web/e2e/office/a.pw.ts"


def test_symlink_component_is_rejected(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(PathDenied):
        resolve_inside(tmp_path, "link/file.ts")
    assert resolve_inside(tmp_path, "real/new/file.ts") == tmp_path / "real" / "new" / "file.ts"
