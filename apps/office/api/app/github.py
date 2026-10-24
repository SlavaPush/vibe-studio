import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

API_VERSION = "2022-11-28"
ACTIONS_APP_ID = 15368
TIMEOUT = 20.0
TOKEN_MARGIN = 300


class GitHubError(Exception):
    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"GitHub {method} {path}: HTTP {status}")
        self.status = status


class GitHubUnavailable(Exception):
    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"GitHub {method} {path}: ответ не получен")


def send(http: httpx.Client, method: str, path: str, *, token: str, bearer: bool = False,
         params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
         links: dict[str, str] | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "Authorization": f"{'Bearer' if bearer else 'token'} {token}",
    }
    response = None
    try:
        response = http.request(method, path, params=params, json=body, headers=headers)
    except httpx.HTTPError:
        pass
    if response is None:
        raise GitHubUnavailable(method, path)
    if response.status_code >= 400:
        raise GitHubError(method, path, response.status_code)
    if links is not None:
        links.update({key: str(value["url"]) for key, value in response.links.items()})
    if response.status_code == 204 or not response.content:
        return None
    data = None
    try:
        data = response.json()
    except ValueError:
        pass
    if data is None:
        raise GitHubError(method, path, response.status_code)
    return data


class AppAuth:
    def __init__(
        self,
        app_id: str,
        installation_id: str,
        key_path: Path,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.key_path = key_path
        self.http = http
        self.clock = clock
        self._lock = threading.Lock()
        self._key: str | None = None
        self._token: str | None = None
        self._expires = 0.0

    def app_jwt(self) -> str:
        if self._key is None:
            self._key = self.key_path.read_text(encoding="utf-8")
        now = int(self.clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self._key, algorithm="RS256")

    def token(self) -> str:
        with self._lock:
            if self._token is not None and self.clock() < self._expires - TOKEN_MARGIN:
                return self._token
            path = f"/app/installations/{self.installation_id}/access_tokens"
            data = send(self.http, "POST", path, token=self.app_jwt(), bearer=True)
            token = data.get("token") if isinstance(data, dict) else None
            expires = data.get("expires_at") if isinstance(data, dict) else None
            if not isinstance(token, str) or not isinstance(expires, str):
                raise GitHubError("POST", path, 200)
            self._token = token
            self._expires = datetime.fromisoformat(expires).timestamp()
            return token


class GitHubClient:
    def __init__(self, http: httpx.Client, auth: AppAuth, repo: str) -> None:
        self.http = http
        self.auth = auth
        self.repo = repo
        self.owner = repo.split("/")[0]

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None, links: dict[str, str] | None = None) -> Any:
        return send(
            self.http, method, f"/repos/{self.repo}{path}", token=self.auth.token(), params=params, body=body,
            links=links,
        )

    def pulls(self, branch: str, state: str) -> list[dict[str, Any]]:
        params = {"head": f"{self.owner}:{branch}", "state": state, "per_page": 100}
        return self._call("GET", "/pulls", params=params) or []

    def create_pr(self, branch: str, title: str, body: str) -> dict[str, Any]:
        return self._call("POST", "/pulls", body={"head": branch, "base": "main", "title": title, "body": body})

    def get_pr(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/pulls/{number}")

    def close_pr(self, number: int) -> dict[str, Any]:
        return self._call("PATCH", f"/pulls/{number}", body={"state": "closed"})

    def merge_pr(self, number: int, sha: str, title: str) -> dict[str, Any]:
        body = {"sha": sha, "merge_method": "squash", "commit_title": title}
        return self._call("PUT", f"/pulls/{number}/merge", body=body)

    def commit(self, ref: str) -> dict[str, Any]:
        return self._call("GET", f"/commits/{ref}")

    def compare(self, base: str, head: str) -> dict[str, Any]:
        return self._call("GET", f"/compare/{base}...{head}")

    def branch_sha(self, branch: str) -> str | None:
        try:
            data = self._call("GET", f"/git/ref/heads/{branch}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        return data["object"]["sha"]

    def runs(self, head_sha: str) -> list[dict[str, Any]]:
        data = self._call("GET", "/actions/runs", params={"head_sha": head_sha, "per_page": 100})
        return data.get("workflow_runs", [])

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        data = self._call("GET", f"/actions/runs/{run_id}/jobs", params={"per_page": 100})
        return data.get("jobs", [])

    def suite_app(self, suite_id: int) -> int | None:
        data = self._call("GET", f"/check-suites/{suite_id}")
        return (data.get("app") or {}).get("id")

    def commit_pulls(self, sha: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/commits/{sha}/pulls", params={"per_page": 100}) or []

    def push_activity(self, pages: int) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"ref": "refs/heads/main", "activity_type": "push", "per_page": 100}
        for _ in range(pages):
            links: dict[str, str] = {}
            yield from self._call("GET", "/activity", params=params, links=links) or []
            following = links.get("next")
            if not following:
                return
            params = dict(httpx.URL(following).params)


def github_client(
    app_id: str,
    installation_id: str,
    key_path: Path,
    repo: str,
    *,
    base_url: str = "https://api.github.com",
    transport: httpx.BaseTransport | None = None,
) -> GitHubClient:
    http = httpx.Client(base_url=base_url, timeout=TIMEOUT, transport=transport)
    return GitHubClient(http, AppAuth(app_id, installation_id, key_path, http), repo)
