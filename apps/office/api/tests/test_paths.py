import pytest

from app.config import POLICIES_DIR
from app.paths import PathDenied, PathPolicy, glob_regex, normalize, resolve_inside
from app.schemas import AgentRole, ChangeClass, Contour

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
        "apps/chat/api/tests/office/conftest.py",
        "apps/chat/web/src/new.spec.ts",
        "apps/chat/web/e2e/office/support/a.pw.ts",
        "apps/chat/web/tsconfig.app.json",
        "apps/chat/web/.oxlintrc.json",
        "apps/chat/web/scripts/check.ts",
        "apps/chat/api/scripts/access_codes.py",
        "apps/chat/api/main.py",
        "apps/chat/api/Dockerfile",
        "apps/chat/api/.env",
        "apps/chat/infra/Dockerfile",
        "apps/chat/web/vite.config.ts",
        "apps/chat/web/yarn.lock",
        "apps/chat/web/e2e/office/a.ts",
        "apps/chat/web/e2e/smoke.pw.ts",
        "apps/chat/web/.env.local",
        "web/src/App.tsx",
        "README.md",
        "CODEOWNERS",
        "package.json",
    ],
)
def test_monorepo_paths_denied(path):
    for change_class in ChangeClass:
        for role in (AgentRole.developer, AgentRole.qa, None):
            with pytest.raises(PathDenied):
                POLICY.check_write(role, path, 10, change_class)
    with pytest.raises(PathDenied):
        POLICY.check_any_writer(path, 10)


SERVER_PATHS = [
    "apps/chat/api/app/main.py",
    "apps/chat/api/app/routing.py",
    "apps/chat/api/app/schema.sql",
    "apps/chat/api/pyproject.toml",
    "apps/chat/contracts/api.md",
    "apps/chat/web/src/api/client.ts",
    "apps/chat/web/src/mocks/contract-examples.ts",
    "apps/chat/web/package.json",
    "apps/chat/web/src/components/chat/ModelSelector.tsx",
]


@pytest.mark.parametrize("path", SERVER_PATHS)
def test_server_zone_is_written_only_by_backend_developer(path):
    assert POLICY.check_write(AgentRole.developer, path, 10, ChangeClass.backend_api) == path
    assert POLICY.check_any_writer(path, 10) == path
    assert POLICY.check_any_writer(path, 10, ChangeClass.backend_api) == path
    with pytest.raises(PathDenied, match="серверной зоны"):
        POLICY.check_write(AgentRole.developer, path, 10, ChangeClass.frontend_ui)
    with pytest.raises(PathDenied):
        POLICY.check_any_writer(path, 10, ChangeClass.frontend_ui)
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.qa, path, 10, ChangeClass.backend_api)
    assert POLICY.contour([path]) == Contour.server
    assert POLICY.contour(["apps/chat/web/src/App.tsx", path]) == Contour.server


CHAT_TEST_PATHS = [
    "apps/chat/api/tests/test_models.py",
    "apps/chat/api/tests/conftest.py",
    "apps/chat/api/tests/data/gateway_cases.json",
    "apps/chat/web/src/components/chat/ModelSelector.test.tsx",
    "apps/chat/web/src/App.test.tsx",
    "apps/chat/web/src/test/fixtures.ts",
    "apps/chat/web/src/test/setup.ts",
    "apps/chat/web/e2e/office/support/mock-api.ts",
    "apps/chat/web/e2e/office/support/mock-api.test.ts",
]


@pytest.mark.parametrize("path", CHAT_TEST_PATHS)
def test_chat_tests_are_written_only_by_backend_developer(path):
    for size in (10, None):
        assert POLICY.check_write(AgentRole.developer, path, size, ChangeClass.backend_api) == path
    assert POLICY.check_any_writer(path, 10) == path
    assert POLICY.check_any_writer(path, None, ChangeClass.backend_api) == path
    for role in (AgentRole.developer, AgentRole.qa, None):
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10, ChangeClass.frontend_ui)
    for role in (AgentRole.qa, None):
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10, ChangeClass.backend_api)
    with pytest.raises(PathDenied):
        POLICY.check_any_writer(path, None, ChangeClass.frontend_ui)
    assert POLICY.is_test(path)
    assert POLICY.contour(["apps/chat/web/src/App.tsx", path]) == Contour.server


@pytest.mark.parametrize(
    "path",
    [
        "apps/chat/api/tests/office/test_tsk_1.py",
        "apps/chat/api/tests/office/conftest.py",
        "apps/chat/api/tests/OFFICE/test_tsk_1.py",
        "apps/chat/web/e2e/office/a.pw.ts",
        "apps/chat/web/e2e/office/support/a.pw.ts",
        "apps/chat/web/src/new.spec.ts",
        "apps/chat/web/vite.config.ts",
        "apps/chat/web/vitest.config.ts",
        "apps/chat/web/tsconfig.app.json",
        "apps/chat/web/.oxlintrc.json",
        "apps/chat/web/scripts/check.ts",
    ],
)
def test_developer_cannot_write_qa_tests_and_build_config(path):
    for change_class in ChangeClass:
        with pytest.raises(PathDenied):
            POLICY.check_write(AgentRole.developer, path, 10, change_class)


def test_non_test_paths_are_not_tests():
    for path in ("apps/chat/web/src/App.tsx", "apps/chat/api/app/main.py", "apps/chat/web/e2e/office/a.pw.ts"):
        assert not POLICY.is_test(path)


def test_interface_paths_have_ui_contour():
    assert POLICY.contour(["apps/chat/web/src/App.tsx", "apps/chat/web/e2e/office/a.pw.ts"]) == Contour.ui
    assert POLICY.contour([]) == Contour.ui
    assert POLICY.check_write(AgentRole.developer, "apps/chat/web/src/App.tsx", 10, ChangeClass.backend_api)


@pytest.mark.parametrize("path", ["apps/chat/web/package-lock.json", "apps/chat/api/uv.lock"])
def test_lock_files_are_written_only_by_office(path):
    size = POLICY.max_file_bytes + 1
    assert POLICY.check_write(None, path, size, ChangeClass.backend_api) == path
    assert POLICY.check_any_writer(path, POLICY.max_lock_bytes) == path
    with pytest.raises(PathDenied):
        POLICY.check_write(None, path, POLICY.max_lock_bytes + 1, ChangeClass.backend_api)
    with pytest.raises(PathDenied):
        POLICY.check_any_writer(path, 10, ChangeClass.frontend_ui)
    for role in (AgentRole.developer, AgentRole.qa):
        with pytest.raises(PathDenied):
            POLICY.check_write(role, path, 10, ChangeClass.backend_api)
    with pytest.raises(PathDenied):
        POLICY.check_write(None, "apps/chat/api/app/main.py", 10, ChangeClass.backend_api)


def test_backend_qa_writes_only_office_pytest():
    path = "apps/chat/api/tests/office/test_tsk_1.py"
    assert POLICY.check_write(AgentRole.qa, path, 10, ChangeClass.backend_api) == path
    for denied in ("apps/chat/api/tests/office/helper.py", "apps/chat/web/e2e/office/a.pw.ts"):
        with pytest.raises(PathDenied):
            POLICY.check_write(AgentRole.qa, denied, 10, ChangeClass.backend_api)
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.qa, path, 10, ChangeClass.frontend_ui)
    with pytest.raises(PathDenied):
        POLICY.check_write(AgentRole.developer, path, 10, ChangeClass.backend_api)


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
