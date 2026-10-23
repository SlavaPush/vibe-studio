from types import SimpleNamespace

import pytest
from conftest import DEMO_CODE, client_for, make_app
from starlette.requests import Request

from app import config
from app.auth import client_ip
from app.config import ConfigError, Settings, app_version, load_settings

SECRETS = {
    "DEMO_ACCESS_CODE": "secret-demo-value",
    "QUOTA_SALT": "secret-salt-value",
    "ANTHROPIC_API_KEY": "secret-anthropic-value",
    "OPENAI_API_KEY": "secret-openai-value",
    "SUPABASE_URL": "https://project.supabase.co",
    "SUPABASE_SECRET_KEY": "secret-storage-value",
    "DATABASE_URL": "postgresql://user:secret-db-value@pooler.test:5432/postgres",
}
NAMES = (
    *SECRETS,
    "RENDER",
    "COOKIE_SECURE",
    "PROXY_HOPS",
    "ALLOWED_ORIGINS",
    "RENDER_EXTERNAL_URL",
    "APP_VERSION",
    "RENDER_GIT_COMMIT",
    "WEB_DIST_DIR",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)

    def set_env(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return set_env


def test_public_environment_starts_with_all_secrets(env):
    env(RENDER="true", **SECRETS)
    settings = load_settings()
    assert settings.cookie_secure is True
    assert settings.demo_access_code == SECRETS["DEMO_ACCESS_CODE"]


@pytest.mark.parametrize("missing", list(SECRETS))
def test_public_environment_requires_secret(env, missing):
    env(RENDER="true", **{name: value for name, value in SECRETS.items() if name != missing})
    with pytest.raises(ConfigError) as error:
        load_settings()
    message = str(error.value)
    assert missing in message
    assert all(value not in message for value in SECRETS.values())


def test_public_environment_rejects_insecure_cookie(env):
    env(RENDER="true", COOKIE_SECURE="false", **SECRETS)
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "COOKIE_SECURE" in str(error.value)
    assert all(value not in str(error.value) for value in SECRETS.values())


def test_local_environment_does_not_require_secrets(env):
    env(COOKIE_SECURE="false")
    settings = load_settings()
    assert settings.cookie_secure is False
    assert settings.allowed_origins == ("http://localhost:5173", "http://127.0.0.1:5173")


def test_render_external_url_is_default_allowed_origin(env):
    env(RENDER_EXTERNAL_URL="https://vibe-chat.onrender.com/")
    assert load_settings().allowed_origins == ("https://vibe-chat.onrender.com",)
    env(ALLOWED_ORIGINS="https://a.test, https://b.test")
    assert load_settings().allowed_origins == ("https://a.test", "https://b.test")


@pytest.mark.parametrize("value", ["0", "-1", "two", ""])
def test_proxy_hops_must_be_positive_integer(env, value):
    env(PROXY_HOPS=value)
    if value == "":
        assert load_settings().proxy_hops == 1
        return
    with pytest.raises(ConfigError, match="PROXY_HOPS"):
        load_settings()


def test_version_prefers_app_version_then_render_commit(env):
    assert app_version() == "dev"
    env(RENDER_GIT_COMMIT="4bd1e4f0123456789abcdef")
    assert app_version() == "4bd1e4f"
    env(APP_VERSION="v-explicit")
    assert app_version() == "v-explicit"


def request_with(forwarded: list[str], trust: bool = True, hops: int = 1) -> Request:
    settings = Settings(trust_proxy_headers=trust, proxy_hops=hops)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    return Request({"type": "http", "app": app, "headers": headers, "client": ("10.0.0.9", 5000)})


def test_client_ip_ignores_forwarded_header_without_trust():
    assert client_ip(request_with(["203.0.113.5"], trust=False)) == "10.0.0.9"


def test_client_ip_takes_address_added_by_nearest_proxy():
    assert client_ip(request_with(["198.51.100.1, 203.0.113.5"])) == "203.0.113.5"
    assert client_ip(request_with(["198.51.100.1, 203.0.113.5, 192.0.2.7"], hops=2)) == "203.0.113.5"


def test_client_ip_joins_repeated_forwarded_headers_in_order():
    assert client_ip(request_with(["198.51.100.1", "203.0.113.5"])) == "203.0.113.5"
    assert client_ip(request_with(["198.51.100.1, 2001:db8::1", "203.0.113.5"], hops=2)) == "2001:db8::1"


def test_client_ip_falls_back_to_connection_address():
    assert client_ip(request_with([])) == "10.0.0.9"
    assert client_ip(request_with(["203.0.113.5"], hops=2)) == "10.0.0.9"
    assert client_ip(request_with(["198.51.100.1, not-an-ip"])) == "10.0.0.9"
    assert client_ip(request_with([""])) == "10.0.0.9"


def test_spoofed_forwarded_prefix_does_not_change_session_source(db):
    with client_for(make_app(db, trust_proxy_headers=True, sessions_per_hour=2)) as client:
        statuses = [
            client.post(
                "/api/demo-session",
                json={"code": DEMO_CODE},
                headers={"X-Forwarded-For": f"198.51.100.{index}, 203.0.113.5"},
            ).status_code
            for index in range(3)
        ]
        other = client.post(
            "/api/demo-session", json={"code": DEMO_CODE}, headers={"X-Forwarded-For": "203.0.113.6"}
        )
    assert statuses == [201, 201, 429]
    assert other.status_code == 201
