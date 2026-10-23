import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Limits:
    runs: int
    images: int
    tokens: int


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql://vibe:vibe@127.0.0.1:5432/vibe"
    demo_access_code: str = ""
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = ""
    openai_base_url: str | None = None
    supabase_url: str = ""
    supabase_secret_key: str = ""
    storage_bucket: str = "images"
    allowed_origins: tuple[str, ...] = tuple(DEFAULT_ORIGINS.split(","))
    cookie_secure: bool = True
    quota_salt: str = ""
    trust_proxy_headers: bool = False
    image_quality: str = "medium"
    user_limits: Limits = field(default_factory=lambda: Limits(60, 5, 1_000_000))
    source_limits: Limits = field(default_factory=lambda: Limits(300, 25, 5_000_000))
    service_limits: Limits = field(default_factory=lambda: Limits(1500, 100, 20_000_000))
    sessions_per_hour: int = 20


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    file_values = load_env_file(ENV_FILE)

    def get(name: str, default: str = "") -> str:
        return file_values.get(name) or os.environ.get(name) or default

    def limits(prefix: str, runs: int, images: int, tokens: int) -> Limits:
        return Limits(
            int(get(f"{prefix}_RUNS_PER_DAY", str(runs))),
            int(get(f"{prefix}_IMAGES_PER_DAY", str(images))),
            int(get(f"{prefix}_TOKENS_PER_DAY", str(tokens))),
        )

    return Settings(
        database_url=get("DATABASE_URL", Settings.database_url),
        demo_access_code=get("DEMO_ACCESS_CODE"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        anthropic_base_url=file_values.get("ANTHROPIC_BASE_URL") or Settings.anthropic_base_url,
        openai_api_key=get("OPENAI_API_KEY"),
        openai_base_url=file_values.get("OPENAI_BASE_URL") or None,
        supabase_url=get("SUPABASE_URL"),
        supabase_secret_key=get("SUPABASE_SECRET_KEY"),
        storage_bucket=get("SUPABASE_STORAGE_BUCKET", Settings.storage_bucket),
        allowed_origins=tuple(
            origin.strip() for origin in get("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if origin.strip()
        ),
        cookie_secure=_flag(get("COOKIE_SECURE", "true")),
        quota_salt=get("QUOTA_SALT"),
        trust_proxy_headers=_flag(get("TRUST_PROXY_HEADERS", "false")),
        image_quality=get("IMAGE_QUALITY", Settings.image_quality),
        user_limits=limits("USER", 60, 5, 1_000_000),
        source_limits=limits("SOURCE", 300, 25, 5_000_000),
        service_limits=limits("SERVICE", 1500, 100, 20_000_000),
        sessions_per_hour=int(get("SESSIONS_PER_HOUR", "20")),
    )
