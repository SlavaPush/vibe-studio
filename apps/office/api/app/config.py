import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_ROOT = REPO_ROOT.parents[1]
POLICIES_DIR = REPO_ROOT / "policies"
SANDBOX_DIR = REPO_ROOT / "docker" / "sandbox"
APP_DIR = "apps/chat"

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
    codes_database_url: str = ""
    codes_check_seconds: float = 10.0
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS
    artifacts_dir: Path = Path("../artifacts")
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    models_policy: Path = POLICIES_DIR / "models.yaml"
    intake_policy: Path = POLICIES_DIR / "intake.yaml"
    session_ttl_hours: int = 168
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
    ollama_url: str = "http://127.0.0.1:11434"
    model_timeout_seconds: float | None = None
    task_time_limit_minutes: float = 60.0
    github_app_id: str = ""
    github_app_installation_id: str = ""
    github_app_private_key_path: Path | None = None
    github_repo: str = "SlavaPush/vibe-studio"
    github_api_url: str = "https://api.github.com"
    github_push_url: str = ""
    checkout_root: Path = CHECKOUT_ROOT
    release_poll_seconds: float = 20.0
    checks_timeout_minutes: float = 30.0
    reconcile_timeout_minutes: float = 30.0
    pr_review_calls: int = 2
    service_url: str = "https://elbrus-vibe-chat.onrender.com"
    render_api_key: str | None = None
    render_service_id: str = ""
    render_api_url: str = "https://api.render.com"
    verify_seconds: float = 120.0
    verify_fault: bool = False
    external_deploy_wait_minutes: float = 30.0
    trusted_github_logins: tuple[str, ...] = ("SlavaPush",)
    github_app_slug: str = "vibe-office"
    rework_limit: int = 1
    owner_login: str = "SlavaPush"

    @property
    def github_app_configured(self) -> bool:
        return bool(self.github_app_id and self.github_app_installation_id and self.github_app_private_key_path)

    @property
    def render_configured(self) -> bool:
        return bool(self.render_api_key and self.render_service_id)

    @property
    def push_url(self) -> str:
        return self.github_push_url or f"https://github.com/{self.github_repo}.git"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_url=env.get("OFFICE_DATABASE_URL") or cls.database_url,
            codes_database_url=env.get("OFFICE_CODES_DATABASE_URL", ""),
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
            ollama_url=env.get("OLLAMA_URL") or cls.ollama_url,
            model_timeout_seconds=float(env["OFFICE_MODEL_TIMEOUT_SECONDS"])
            if env.get("OFFICE_MODEL_TIMEOUT_SECONDS")
            else None,
            task_time_limit_minutes=float(
                env.get("OFFICE_TASK_TIME_LIMIT_MINUTES") or cls.task_time_limit_minutes
            ),
            github_app_id=env.get("GITHUB_APP_ID", ""),
            github_app_installation_id=env.get("GITHUB_APP_INSTALLATION_ID", ""),
            github_app_private_key_path=Path(env["GITHUB_APP_PRIVATE_KEY_PATH"])
            if env.get("GITHUB_APP_PRIVATE_KEY_PATH")
            else None,
            github_repo=env.get("OFFICE_GITHUB_REPO") or cls.github_repo,
            release_poll_seconds=float(env.get("OFFICE_RELEASE_POLL_SECONDS") or cls.release_poll_seconds),
            checks_timeout_minutes=float(
                env.get("OFFICE_CHECKS_TIMEOUT_MINUTES") or cls.checks_timeout_minutes
            ),
            reconcile_timeout_minutes=float(
                env.get("OFFICE_RECONCILE_TIMEOUT_MINUTES") or cls.reconcile_timeout_minutes
            ),
            pr_review_calls=int(env.get("OFFICE_PR_REVIEW_CALLS") or cls.pr_review_calls),
            service_url=env.get("OFFICE_SERVICE_URL") or cls.service_url,
            render_api_key=env.get("RENDER_API_KEY") or None,
            render_service_id=env.get("RENDER_SERVICE_ID", ""),
            verify_seconds=float(env.get("OFFICE_VERIFY_SECONDS") or cls.verify_seconds),
            verify_fault=env.get("OFFICE_VERIFY_FAULT") == "1",
            external_deploy_wait_minutes=float(
                env.get("OFFICE_EXTERNAL_DEPLOY_WAIT_MINUTES") or cls.external_deploy_wait_minutes
            ),
            trusted_github_logins=_list(env.get("OFFICE_TRUSTED_GITHUB_LOGINS"), cls.trusted_github_logins),
            github_app_slug=env.get("OFFICE_GITHUB_APP_SLUG") or cls.github_app_slug,
            rework_limit=int(env.get("OFFICE_REWORK_LIMIT") or cls.rework_limit),
            owner_login=env.get("OFFICE_OWNER_LOGIN") or cls.owner_login,
        )
