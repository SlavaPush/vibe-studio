from typing import Any

import httpx

TIMEOUT = 30.0
LIVE = "live"
FAILED = frozenset({"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"})


class RenderError(Exception):
    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"Render {method} {path}: HTTP {status}")
        self.status = status


class RenderUnavailable(Exception):
    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"Render {method} {path}: ответ не получен")


def is_terminal(status: str | None) -> bool:
    return status == LIVE or status in FAILED


class RenderClient:
    def __init__(self, http: httpx.Client, api_key: str, service_id: str) -> None:
        self.http = http
        self.api_key = api_key
        self.service_id = service_id

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None) -> Any:
        return self._request(method, f"/v1/services/{self.service_id}{path}", params=params, body=body)

    def _request(self, method: str, full: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"}
        response = None
        try:
            response = self.http.request(method, full, params=params, json=body, headers=headers)
        except httpx.HTTPError:
            pass
        if response is None:
            raise RenderUnavailable(method, full)
        if response.status_code >= 400:
            raise RenderError(method, full, response.status_code)
        data = None
        try:
            data = response.json()
        except ValueError:
            pass
        if not isinstance(data, (dict, list)):
            raise RenderUnavailable(method, full)
        return data

    def deploy(self, commit_sha: str) -> dict[str, Any]:
        return self._call("POST", "/deploys", body={"commitId": commit_sha})

    def get_deploy(self, deploy_id: str) -> dict[str, Any]:
        return self._call("GET", f"/deploys/{deploy_id}")

    def list_deploys(self, limit: int = 20) -> list[dict[str, Any]]:
        data = self._call("GET", "/deploys", params={"limit": limit})
        return [item["deploy"] for item in data if isinstance(item, dict) and isinstance(item.get("deploy"), dict)]

    def rollback(self, deploy_id: str) -> dict[str, Any]:
        return self._call("POST", "/rollback", body={"deployId": deploy_id})

    def service(self) -> dict[str, Any]:
        return self._call("GET", "")

    def logs(self, owner_id: str, start: str | None, end: str | None, limit: int = 100) -> dict[str, Any]:
        params = {
            "ownerId": owner_id, "resource": self.service_id, "direction": "forward", "limit": limit,
            "startTime": start, "endTime": end,
        }
        data = self._request("GET", "/v1/logs", params={key: value for key, value in params.items() if value})
        if not isinstance(data, dict):
            raise RenderUnavailable("GET", "/v1/logs")
        return data


def render_client(
    api_key: str, service_id: str, *, base_url: str = "https://api.render.com",
    transport: httpx.BaseTransport | None = None,
) -> RenderClient:
    return RenderClient(httpx.Client(base_url=base_url, timeout=TIMEOUT, transport=transport), api_key, service_id)
