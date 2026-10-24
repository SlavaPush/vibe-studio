import json
from pathlib import Path

import httpx
import pytest

from app.github import github_client
from app.render import RenderError, RenderUnavailable, is_terminal, render_client
from app.service_check import BaseUnavailable, ServiceCheck

DATA = Path(__file__).with_name("data")
KEY = "rnd_secret_value"


def client(handler):
    return render_client(KEY, "srv-dbdtap5g1s2s73b13edg", transport=httpx.MockTransport(handler))


def test_requests_follow_render_api_and_parse_saved_list():
    seen = []
    listed = json.loads((DATA / "render" / "deploys.json").read_text(encoding="utf-8"))

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.content))
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        if request.method == "GET" and request.url.path.endswith("/deploys"):
            return httpx.Response(200, json=listed)
        return httpx.Response(201 if request.method == "POST" else 200, json={"id": "dep-1", "status": "created"})

    render = client(handler)
    deploys = render.list_deploys()
    assert [(item["id"], item["status"], item["commit"]["id"][:7]) for item in deploys] == [
        ("dep-dbeaqtvavr4c73aa26u0", "live", "7f9f17c"),
        ("dep-dbeanncs728c73bkdlmg", "deactivated", "e511da6"),
        ("dep-dbea7o3ncjis73dj3h30", "deactivated", "de4bfc1"),
    ]
    render.deploy("a" * 40)
    render.get_deploy("dep-1")
    render.rollback("dep-dbeanncs728c73bkdlmg")
    base = "/v1/services/srv-dbdtap5g1s2s73b13edg"
    assert [(method, path) for method, path, _, _ in seen] == [
        ("GET", f"{base}/deploys"), ("POST", f"{base}/deploys"), ("GET", f"{base}/deploys/dep-1"),
        ("POST", f"{base}/rollback"),
    ]
    assert seen[0][2] == {"limit": "20"}
    assert json.loads(seen[1][3]) == {"commitId": "a" * 40}
    assert json.loads(seen[3][3]) == {"deployId": "dep-dbeanncs728c73bkdlmg"}


@pytest.mark.parametrize("failure", ["status", "timeout", "garbled"])
def test_errors_do_not_contain_key(failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "garbled":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(401, json={"message": f"bad key {KEY}"})

    expected = RenderError if failure == "status" else RenderUnavailable
    with pytest.raises(expected) as raised:
        client(handler).deploy("a" * 40)
    assert KEY not in f"{raised.value} {raised.value!r}"
    assert raised.value.__cause__ is None
    if failure == "status":
        assert raised.value.status == 401


def test_terminal_statuses():
    assert is_terminal("live")
    for status in ("build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"):
        assert is_terminal(status)
    for status in ("created", "queued", "build_in_progress", "update_in_progress", "pre_deploy_in_progress", None):
        assert not is_terminal(status)


def service(handler):
    return ServiceCheck(httpx.Client(transport=httpx.MockTransport(handler)), "https://chat.example/")


def test_service_check_reads_health_version_and_page():
    def handler(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "7f9f17c"})
        assert request.headers["Cache-Control"] == "no-cache"
        return httpx.Response(200, text='<head>\n    <meta name="app-version" content="7f9f17c" />\n</head>')

    check = service(handler)
    assert (check.health(), check.version(), check.page_version()) == (True, "7f9f17c", "7f9f17c")


def test_service_check_failures():
    def handler(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "starting"})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "dev"})
        return httpx.Response(200, text='<meta name="app-version" content="dev" />')

    check = service(handler)
    assert check.health() is False
    assert check.page_version() is None
    with pytest.raises(BaseUnavailable):
        check.version()
    down = service(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)))
    assert (down.health(), down.page_version()) == (False, None)


def test_push_activity_sample_and_pages(app_key):
    sample = json.loads((DATA / "github" / "activity.json").read_text(encoding="utf-8"))
    calls = []

    def handler(request):
        if request.url.path.startswith("/app/installations/"):
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        calls.append(dict(request.url.params))
        if "after" in request.url.params:
            return httpx.Response(200, json=sample)
        link = '<https://api.github.com/repos/SlavaPush/vibe-studio/activity?per_page=100&after=Y3Vyc29y>; rel="next"'
        return httpx.Response(200, json=sample, headers={"Link": link})

    github = github_client("1", "2", app_key.path, "SlavaPush/vibe-studio", transport=httpx.MockTransport(handler))
    items = list(github.push_activity(3))
    assert [item["actor"]["login"] for item in items] == ["SlavaPush", "SlavaPush"]
    assert calls == [
        {"ref": "refs/heads/main", "activity_type": "push", "per_page": "100"},
        {"per_page": "100", "after": "Y3Vyc29y"},
    ]
