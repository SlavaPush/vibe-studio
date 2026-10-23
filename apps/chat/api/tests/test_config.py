import hashlib
import hmac
import logging
from types import SimpleNamespace

import pytest
from conftest import DEMO_CODE, add_code, client_for, make_app
from starlette.requests import Request

from app import config
from app.auth import client_ip
from app.config import ConfigError, Settings, app_version, load_settings

SECRETS = {
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
    "CLIENT_IP_SOURCE",
    "LOG_FORWARDED_CHAIN",
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
    assert settings.database_url == SECRETS["DATABASE_URL"]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:secret-db-value@localhost:5432/postgres",
        "postgresql://user:secret-db-value@127.0.0.1:5432/postgres",
        "postgresql://user:secret-db-value@[::1]:5432/postgres",
        "postgresql://user:secret-db-value@pooler.test,LOCALHOST:5432/postgres",
        "host=127.0.0.1 password=secret-db-value dbname=postgres",
        "postgresql://user:secret-db-value@/postgres",
        "secret-db-value",
    ],
)
def test_public_environment_rejects_local_database(env, url):
    env(RENDER="true", **{**SECRETS, "DATABASE_URL": url})
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "DATABASE_URL" in str(error.value)
    assert "secret-db-value" not in str(error.value)


def test_local_environment_allows_local_database(env):
    env(DATABASE_URL="postgresql://vibe:vibe@127.0.0.1:5432/vibe")
    assert load_settings().database_url == "postgresql://vibe:vibe@127.0.0.1:5432/vibe"


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


def request_with(
    forwarded: list[str],
    trust: bool = True,
    hops: int = 1,
    source: str = "xff_from_end",
    extra: dict[str, str] | None = None,
) -> Request:
    settings = Settings(trust_proxy_headers=trust, proxy_hops=hops, client_ip_source=source)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    headers += [(name.encode(), value.encode()) for name, value in (extra or {}).items()]
    return Request({"type": "http", "app": app, "headers": headers, "client": ("10.0.0.9", 5000)})


def test_client_ip_source_defaults_to_proxy_hops_and_rejects_unknown_value(env):
    assert load_settings().client_ip_source == "xff_from_end"
    env(CLIENT_IP_SOURCE=" XFF_First ")
    assert load_settings().client_ip_source == "xff_first"
    env(CLIENT_IP_SOURCE="x-forwarded-host")
    with pytest.raises(ConfigError, match="CLIENT_IP_SOURCE"):
        load_settings()


def test_client_ip_takes_first_forwarded_element():
    assert client_ip(request_with(["203.0.113.5, 198.51.100.1, 10.0.0.1"], source="xff_first")) == "203.0.113.5"
    assert client_ip(request_with(["203.0.113.5", "10.0.0.1"], source="xff_first")) == "203.0.113.5"
    assert client_ip(request_with([], source="xff_first")) == "10.0.0.9"
    assert client_ip(request_with(["not-an-ip, 203.0.113.5"], source="xff_first")) == "10.0.0.9"
    assert client_ip(request_with(["203.0.113.5"], trust=False, source="xff_first")) == "10.0.0.9"


@pytest.mark.parametrize("header", ["true-client-ip", "cf-connecting-ip", "x-real-ip"])
def test_client_ip_takes_named_header(header):
    request = request_with(["198.51.100.1, 192.0.2.7"], source=header, extra={header: " 203.0.113.5 "})
    assert client_ip(request) == "203.0.113.5"
    assert client_ip(request_with(["198.51.100.1"], source=header)) == "10.0.0.9"
    assert client_ip(request_with([], source=header, extra={header: "unknown"})) == "10.0.0.9"
    assert client_ip(request_with([], trust=False, source=header, extra={header: "203.0.113.5"})) == "10.0.0.9"


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


def fresh_codes(db) -> list[str]:
    codes = [f"code-{index}" for index in range(4)]
    for code in codes:
        add_code(db, code, code)
    return codes


def test_spoofed_forwarded_prefix_does_not_change_session_source(db):
    codes = fresh_codes(db)
    with client_for(make_app(db, trust_proxy_headers=True, sessions_per_hour=2)) as client:
        statuses = [
            client.post(
                "/api/demo-session",
                json={"code": codes[index]},
                headers={"X-Forwarded-For": f"198.51.100.{index}, 203.0.113.5"},
            ).status_code
            for index in range(3)
        ]
        other = client.post(
            "/api/demo-session", json={"code": codes[3]}, headers={"X-Forwarded-For": "203.0.113.6"}
        )
    assert statuses == [201, 201, 429]
    assert other.status_code == 201


def test_spoofed_forwarded_suffix_does_not_change_session_source_with_first_element(db):
    codes = fresh_codes(db)
    app = make_app(db, trust_proxy_headers=True, client_ip_source="xff_first", sessions_per_hour=2)
    with client_for(app) as client:
        statuses = [
            client.post(
                "/api/demo-session",
                json={"code": codes[index]},
                headers={"X-Forwarded-For": f"203.0.113.5, 198.51.100.{index}, 10.0.0.{index}"},
            ).status_code
            for index in range(3)
        ]
        other = client.post(
            "/api/demo-session", json={"code": codes[3]}, headers={"X-Forwarded-For": "203.0.113.6, 198.51.100.1"}
        )
    assert statuses == [201, 201, 429]
    assert other.status_code == 201


def test_forwarded_chain_is_not_logged_by_default(db, caplog):
    caplog.set_level(logging.INFO)
    with client_for(make_app(db, trust_proxy_headers=True)) as client:
        client.post("/api/demo-session", json={"code": DEMO_CODE}, headers={"X-Forwarded-For": "203.0.113.5"})
    assert not [record for record in caplog.records if "forwarded chain" in record.getMessage()]


def test_forwarded_chain_log_has_hashes_without_addresses(db, caplog):
    caplog.set_level(logging.INFO)
    addresses = ("203.0.113.5", "198.51.100.7", "10.0.0.1", "192.0.2.44", "2001:db8::9")
    app = make_app(db, trust_proxy_headers=True, log_forwarded_chain=True)
    with client_for(app) as client:
        response = client.post(
            "/api/demo-session",
            json={"code": DEMO_CODE},
            headers=[
                ("X-Forwarded-For", f"{addresses[0]}, {addresses[1]}"),
                ("X-Forwarded-For", addresses[2]),
                ("True-Client-IP", addresses[3]),
                ("Forwarded", f'for="[{addresses[4]}]"'),
            ],
        )
    assert response.status_code == 201
    records = [record for record in caplog.records if "forwarded chain" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    line = records[0].getMessage()
    assert "count=3" in line
    salt = app.state.settings.quota_salt.encode()
    for address in addresses[:4]:
        assert hmac.new(salt, address.encode(), hashlib.sha256).hexdigest()[:8] in line
    assert "'true-client-ip'" in line and "'forwarded'" in line and "x-real-ip" not in line
    assert all(address not in line for address in addresses)
