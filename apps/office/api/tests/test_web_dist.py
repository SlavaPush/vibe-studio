import pytest

INDEX = "<!doctype html><html><head><title>Панель офиса</title></head><body></body></html>"
API_ERROR_KEYS = {"code", "message", "retryable", "operation_id"}
ASSET_CACHE = "public, max-age=31536000, immutable"


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (root / "theme-init.js").write_text("theme()", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    (root / "assets" / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    return root


@pytest.fixture
def web(office, dist):
    return office.client(office.new_app(web_dist_dir=dist))


@pytest.mark.parametrize("path", ["/", "/tasks/x", "/some/deep/path"])
def test_pages_return_index_without_cache(web, path):
    response = web.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == INDEX
    head = web.head(path)
    assert head.status_code == 200
    assert head.headers["content-type"].startswith("text/html")
    assert head.headers["cache-control"] == "no-cache"


def test_root_files_and_assets_are_served(web):
    theme = web.get("/theme-init.js")
    assert theme.status_code == 200
    assert theme.text == "theme()"
    assert web.get("/favicon.svg").text == "<svg></svg>"
    asset = web.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"
    assert asset.headers["cache-control"] == ASSET_CACHE


def test_api_still_works_with_panel(web):
    assert web.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api", "/api/nope", "/assets/missing.js", "/assets/link.txt"])
def test_unknown_api_and_asset_paths_return_api_error(web, path):
    response = web.get(path)
    assert response.status_code == 404
    assert set(response.json()) == API_ERROR_KEYS
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", ["/", "/tasks/x"])
def test_unknown_non_get_request_returns_api_error(web, path):
    response = web.post(path, json={})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
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
def test_files_outside_build_are_not_served(web, path):
    assert "outside" not in web.get(path).text


def test_panel_requires_allowed_host(office, dist):
    client = office.client(office.new_app(web_dist_dir=dist))
    response = client.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_origin"


def test_without_web_dist_dir_panel_is_not_served(office):
    client = office.client()
    response = client.get("/")
    assert response.status_code == 404
    assert set(response.json()) == API_ERROR_KEYS
    assert response.json()["code"] == "not_found"
    assert client.get("/assets/index.js").status_code == 404
