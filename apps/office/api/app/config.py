import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_DIR = REPO_ROOT / "policies"
SANDBOX_DIR = REPO_ROOT / "docker" / "sandbox"

DEFAULT_HOSTS = ("127.0.0.1:8001", "localhost:8001", "127.0.0.1:5173", "localhost:5173")
DEFAULT_ORIGINS = (
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


def _list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./office.sqlite"
    access_code: str = ""
    admin_code: str = ""
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS
    artifacts_dir: Path = Path("../artifacts")
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    models_policy: Path = POLICIES_DIR / "models.yaml"
    intake_policy: Path = POLICIES_DIR / "intake.yaml"
    session_ttl_hours: int = 12
    login_failures_per_minute: int = 5
    tasks_per_hour: int = 5
    intake_timeout_seconds: float = 30.0
    sse_ping_seconds: float = 15.0
    sse_poll_seconds: float = 1.0
    target_repo_url: str = ""
    workspace_dir: Path = Path("../workspace")
    paths_policy: Path = POLICIES_DIR / "paths.yaml"
    commands_policy: Path = POLICIES_DIR / "commands.yaml"
    sandbox_image: str = "back-office-sandbox:p4"
    sandbox_containers: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_url=env.get("OFFICE_DATABASE_URL") or cls.database_url,
            access_code=env.get("OFFICE_ACCESS_CODE", ""),
            admin_code=env.get("OFFICE_ADMIN_CODE", ""),
            allowed_hosts=_list(env.get("OFFICE_ALLOWED_HOSTS"), DEFAULT_HOSTS),
            allowed_origins=_list(env.get("OFFICE_ALLOWED_ORIGINS"), DEFAULT_ORIGINS),
            artifacts_dir=Path(env.get("OFFICE_ARTIFACTS_DIR") or "../artifacts"),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
            openai_api_key=env.get("OPENAI_API_KEY") or None,
            anthropic_base_url=env.get("OFFICE_ANTHROPIC_BASE_URL") or cls.anthropic_base_url,
            target_repo_url=env.get("TARGET_REPO_URL", ""),
            workspace_dir=Path(env.get("WORKSPACE_DIR") or "../workspace"),
            sandbox_image=env.get("OFFICE_SANDBOX_IMAGE") or cls.sandbox_image,
            sandbox_containers=int(env.get("OFFICE_SANDBOX_CONTAINERS") or cls.sandbox_containers),
        )
