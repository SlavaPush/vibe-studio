import threading

import pytest

from app.sessions import COOKIE_NAME, token_hash
from conftest import ACCESS_CODE, ADMIN_CODE

PROTECTED = [
    ("get", "/api/me"),
    ("get", "/api/tasks"),
    ("post", "/api/tasks"),
    ("get", "/api/tasks/tsk_x"),
    ("get", "/api/tasks/tsk_x/events"),
    ("get", "/api/events"),
    ("get", "/api/tasks/tsk_x/checks"),
    ("get", "/api/tasks/tsk_x/artifacts/build.log"),
    ("post", "/api/tasks/tsk_x/decisions"),
    ("get", "/api/releases"),
    ("post", "/api/releases/rel_x/decisions"),
    ("get", "/api/slots"),
]

BODIES = {
    "/api/tasks": {"text": "Добавить кнопку копирования ответа в чате vibe-chat.", "profile": "hybrid"},
    "/api/tasks/tsk_x/decisions": {"decision": "cancel", "expected_version": 1, "idempotency_key": "key-0001"},
    "/api/releases/rel_x/decisions": {
        "decision": "approve_release",
        "expected_version": 1,
        "idempotency_key": "key-0002",
    },
}


@pytest.mark.parametrize(("admin", "role"), [(False, "participant"), (True, "admin")])
def test_login_sets_cookie_and_stores_only_hash(office, admin, role):
    client = office.client()
    response = client.post(
        "/api/office-session",
        json={"access_code": ADMIN_CODE if admin else ACCESS_CODE, "display_name": "Мария"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == role
    assert body["id"].startswith("prt_")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    token = client.cookies[COOKIE_NAME]
    with office.store.db.connection() as conn:
        rows = conn.fetchall("SELECT token_hash FROM sessions")
    assert [row["token_hash"] for row in rows] == [token_hash(token)]
    assert token not in str(rows)
    assert client.get("/api/me").json()["id"] == body["id"]


def test_wrong_code_and_rate_limit(office):
    client = office.client()
    for _ in range(5):
        response = client.post("/api/office-session", json={"access_code": "wrong", "display_name": "Мария"})
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_access_code"
    response = client.post("/api/office-session", json={"access_code": ACCESS_CODE, "display_name": "Мария"})
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert response.json()["retryable"] is True
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_parallel_wrong_codes_respect_limit(office):
    client = office.client()
    for _ in range(4):
        client.post("/api/office-session", json={"access_code": "wrong", "display_name": "Мария"})
    barrier = threading.Barrier(6)
    statuses = []

    def attempt():
        barrier.wait()
        response = client.post("/api/office-session", json={"access_code": "wrong", "display_name": "Мария"})
        statuses.append((response.status_code, response.json()["code"]))

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [(401, "invalid_access_code")] + [(429, "rate_limited")] * 5


def test_empty_codes_forbid_login(office):
    app = office.new_app(access_code="", admin_code="")
    client = office.client(app)
    response = client.post("/api/office-session", json={"access_code": "", "display_name": "Мария"})
    assert response.status_code == 422
    response = client.post("/api/office-session", json={"access_code": "x", "display_name": "Мария"})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_routes_require_session(office, method, path):
    client = office.client()
    kwargs = {"json": BODIES[path]} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_health_and_login_do_not_require_session(office):
    client = office.client()
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/office-session", json={"access_code": ACCESS_CODE, "display_name": "Ира"}).status_code == 200


def test_same_name_gives_independent_participants(office):
    first, me = office.login("Мария")
    second, other = office.login("Мария")
    assert me["id"] != other["id"]
    task = office.post_task(first).json()
    response = second.post(
        f"/api/tasks/{task['id']}/decisions",
        json={"decision": "cancel", "expected_version": task["version"], "idempotency_key": "foreign-cancel"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_not_owner"


def test_relogin_with_valid_cookie_keeps_participant(office):
    client, me = office.login("Мария")
    old_token = client.cookies[COOKIE_NAME]
    response = client.post("/api/office-session", json={"access_code": ACCESS_CODE, "display_name": "Мария П."})
    assert response.status_code == 200
    assert response.json()["id"] == me["id"]
    assert response.json()["display_name"] == "Мария П."
    assert client.cookies[COOKIE_NAME] != old_token
    stale = office.client()
    assert stale.get("/api/me", headers={"Cookie": f"{COOKIE_NAME}={old_token}"}).status_code == 401


def test_relogin_with_other_role_creates_new_participant(office):
    client, me = office.login("Мария")
    response = client.post("/api/office-session", json={"access_code": ADMIN_CODE, "display_name": "Мария"})
    assert response.json()["id"] != me["id"]
    assert response.json()["role"] == "admin"


def test_expired_session_is_rejected(office):
    client, _ = office.login()
    with office.store.db.transaction() as conn:
        conn.execute("UPDATE sessions SET expires_at = :past", {"past": "2020-01-01T00:00:00.000000Z"})
    assert client.get("/api/me").status_code == 401
