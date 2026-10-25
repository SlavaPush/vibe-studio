import secrets
from pathlib import Path

import httpx
import jwt
import pytest

from app.config import CHECKOUT_ROOT, Settings
from app.github import API_VERSION, AppAuth, GitHubClient, GitHubError, GitHubUnavailable, github_client
from app.required_checks import load_required_checks
from conftest import FakeGitHub


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def client_for(fake: FakeGitHub, key: Path, clock: Clock | None = None) -> GitHubClient:
    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler))
    auth = AppAuth("5452356", "174735588", key, http, clock or Clock())
    return GitHubClient(http, auth, "SlavaPush/vibe-studio")


def test_installation_token_is_cached_until_five_minutes_before_expiry(app_key, tmp_path):
    clock = Clock()
    responses = []

    def handler(request: httpx.Request) -> httpx.Response:
        responses.append(request)
        expires = "2027-01-15T09:00:00Z" if len(responses) == 1 else "2027-01-15T10:00:00Z"
        return httpx.Response(201, json={"token": f"token-{len(responses)}", "expires_at": expires})

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    expiry = 1_800_000_000.0 + 3600
    auth = AppAuth("5452356", "174735588", app_key.path, http, clock)
    first = auth.token()
    clock.now = expiry - 301
    assert auth.token() == first
    clock.now = expiry - 299
    assert auth.token() == "token-2"
    assert len(responses) == 2
    request = responses[0]
    assert request.method == "POST"
    assert request.url.path == "/app/installations/174735588/access_tokens"
    assert request.headers["X-GitHub-Api-Version"] == API_VERSION
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, app_key.public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "5452356"
    assert claims["exp"] - claims["iat"] == 600
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_requests_carry_api_version_and_installation_token(app_key, tmp_path):
    token = secrets.token_hex(16)
    fake = FakeGitHub(tmp_path, token)
    client = client_for(fake, app_key.path)
    assert client.pulls("office/tsk_1", "open") == []
    request = fake.requests[-1]
    assert request.url.path == "/repos/SlavaPush/vibe-studio/pulls"
    assert request.url.params["head"] == "SlavaPush:office/tsk_1"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == API_VERSION
    assert request.headers["Authorization"] == f"token {token}"


def test_missing_branch_reads_as_none(app_key, tmp_path, origin):
    fake = FakeGitHub(origin.bare, "t")
    assert client_for(fake, app_key.path).branch_sha("office/tsk_missing") is None


def test_errors_hide_token_and_have_no_chained_exception(app_key, tmp_path):
    token = secrets.token_hex(16)
    fake = FakeGitHub(tmp_path, token)
    client = client_for(fake, app_key.path)
    client.auth.token()
    jwt_value = fake.jwts[-1]
    fake.status[("GET", "/repos/SlavaPush/vibe-studio/pulls")] = 502
    with pytest.raises(GitHubError) as failed:
        client.pulls("office/tsk_1", "open")
    fake.down = True
    with pytest.raises(GitHubUnavailable) as unavailable:
        client.get_pr(3)
    for error in (failed.value, unavailable.value):
        text = f"{error} {error!r} {error.args}"
        assert token not in text and jwt_value not in text
        assert "?" not in str(error)
        assert error.__cause__ is None and error.__context__ is None
    assert failed.value.status == 502


def test_factory_sets_timeout_and_base_url(app_key):
    client = github_client("1", "2", app_key.path, "SlavaPush/vibe-studio")
    assert client.http.timeout.read == 20.0
    assert str(client.http.base_url) == "https://api.github.com"


def test_required_checks_come_from_ruleset_and_workflows():
    checks = {check.name: check for check in load_required_checks(CHECKOUT_ROOT)}
    assert set(checks) == {"chat-web", "chat-containers", "office-paths"}
    assert checks["chat-web"].workflow == ".github/workflows/ci.yml"
    assert (checks["chat-web"].pr_event, checks["chat-web"].push_event) == ("pull_request", "push")
    assert checks["office-paths"].workflow == ".github/workflows/office-paths.yml"
    assert (checks["office-paths"].pr_event, checks["office-paths"].push_event) == ("pull_request_target", None)


def test_required_check_without_workflow_is_an_error(tmp_path):
    (tmp_path / ".github" / "rulesets").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir()
    (tmp_path / ".github" / "rulesets" / "main.json").write_text(
        '{"rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": '
        '[{"context": "lint", "integration_id": 15368}]}}]}'
    )
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\njobs:\n  lint:\n    runs-on: x\n")
    with pytest.raises(ValueError, match="lint"):
        load_required_checks(tmp_path)


def test_settings_read_github_app_and_render(monkeypatch, tmp_path):
    for name in ("OFFICE_GITHUB_REPO", "OFFICE_RELEASE_POLL_SECONDS", "OFFICE_CHECKS_TIMEOUT_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "5452356")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "174735588")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "app.pem"))
    monkeypatch.setenv("RENDER_API_KEY", "render-key")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-1")
    settings = Settings.from_env()
    assert settings.github_app_configured
    assert settings.github_app_private_key_path == tmp_path / "app.pem"
    assert settings.github_repo == "SlavaPush/vibe-studio"
    assert settings.push_url == "https://github.com/SlavaPush/vibe-studio.git"
    assert (settings.release_poll_seconds, settings.checks_timeout_minutes) == (20.0, 30.0)
    assert (settings.render_api_key, settings.render_service_id) == ("render-key", "srv-1")
    monkeypatch.setenv("OFFICE_GITHUB_REPO", "owner/other")
    monkeypatch.setenv("OFFICE_RELEASE_POLL_SECONDS", "5")
    monkeypatch.setenv("OFFICE_CHECKS_TIMEOUT_MINUTES", "10")
    monkeypatch.delenv("GITHUB_APP_ID")
    settings = Settings.from_env()
    assert not settings.github_app_configured
    assert (settings.github_repo, settings.release_poll_seconds, settings.checks_timeout_minutes) == (
        "owner/other", 5.0, 10.0
    )
