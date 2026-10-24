import pytest

from conftest import BASE_URL, UI_TASK


def assert_forbidden_origin(response):
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_origin"


def test_foreign_host_is_rejected(office):
    client = office.client()
    assert_forbidden_origin(client.get("/api/health", headers={"Host": "evil.example"}))


def test_foreign_host_is_rejected_on_sse(office):
    client, _ = office.login()
    assert_forbidden_origin(client.get("/api/events", headers={"Host": "evil.example"}))


@pytest.mark.parametrize("origin", ["https://evil.example", "null", None])
def test_mutating_request_with_bad_origin_is_rejected(office, origin):
    client, _ = office.login()
    headers = {"Origin": origin} if origin else {}
    if origin is None:
        client.headers.pop("Origin")
    login = {"access_code": office.add_code("Ира")}
    assert_forbidden_origin(client.post("/api/office-session", json=login, headers=headers))
    assert office.code_row("Ира")["office_used_at"] is None
    assert_forbidden_origin(client.post("/api/tasks", json={"text": UI_TASK, "profile": "hybrid"}, headers=headers))
    assert office.openai.moderation_calls == []


def test_allowed_host_and_origin_pass(office):
    client = office.client()
    login = {"access_code": office.add_code("Мария")}
    assert client.post("/api/office-session", json=login, headers={"Origin": BASE_URL}).status_code == 200
    assert client.get("/api/me").status_code == 200


def test_get_without_origin_passes(office):
    client, _ = office.login()
    client.headers.pop("Origin")
    assert client.get("/api/me").status_code == 200
