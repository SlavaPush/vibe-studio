import pytest
from conftest import settings_for
from fastapi.testclient import TestClient

from app.main import create_app

INDEX = '<!doctype html><html><head><meta name="app-version" content="dev" /></head><body></body></html>'
API_ERROR_KEYS = {"code", "message", "retryable", "operation_id"}


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    (root / "assets" / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    return root


def web_client(dist=None, **overrides) -> TestClient:
    settings = settings_for("postgresql://unused@127.0.0.1:1/unused", web_dist_dir=str(dist or ""), **overrides)
    return TestClient(create_app(settings))


def test_index_is_served_with_server_version(dist, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "4bd1e4f")
    client = web_client(dist)
    for path in ("/", "/conversations/123", "/some/deep/path"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        assert '<meta name="app-version" content="4bd1e4f" />' in response.text
    assert client.get("/api/version").json() == {"version": "4bd1e4f"}


def test_assets_have_long_cache_and_root_files_are_served(dist):
    client = web_client(dist)
    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    favicon = client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.text == "<svg></svg>"


@pytest.mark.parametrize("path", ["/api/unknown", "/api", "/assets/missing.js", "/assets/link.txt"])
def test_unknown_api_and_asset_paths_return_api_error(dist, path):
    response = web_client(dist).get(path)
    assert response.status_code == 404
    assert set(response.json()) == API_ERROR_KEYS
    assert response.json()["code"] == "not_found"


def test_unknown_non_get_request_returns_api_error(dist):
    response = web_client(dist).post("/some/page", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/link.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/..%2fsecret.txt",
    ],
)
def test_files_outside_build_are_not_served(dist, path):
    response = web_client(dist).get(path)
    assert "outside" not in response.text


def test_without_web_dist_dir_interface_is_not_served():
    client = web_client()
    response = client.get("/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert client.get("/assets/index.js").status_code == 404


def test_security_headers_on_page_and_api(dist):
    client = web_client(dist, supabase_url="https://project.supabase.co")
    for response in (client.get("/"), client.get("/api/version"), client.get("/api/unknown")):
        policy = response.headers["content-security-policy"]
        assert "script-src 'self';" in policy
        assert "unsafe-inline" not in policy.split("script-src", 1)[1].split(";", 1)[0]
        assert "img-src 'self' data: blob: https://project.supabase.co;" in policy
        assert "frame-ancestors 'none'" in policy
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
