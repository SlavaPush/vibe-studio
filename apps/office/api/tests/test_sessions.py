import threading
from datetime import timedelta

import pytest

from app.db import to_text, utcnow
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


PAST = "2020-01-01T00:00:00.000000Z"


def with_token(token):
    return {"Cookie": f"{COOKIE_NAME}={token}"}


def add_session(office, participant_id, token):
    now = utcnow()
    with office.store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, participant_id, created_at, expires_at) "
            "VALUES (:hash, :participant, :now, :expires)",
            {
                "hash": token_hash(token),
                "participant": participant_id,
                "now": to_text(now),
                "expires": to_text(now + timedelta(hours=1)),
            },
        )


def session_hashes(office):
    with office.store.db.connection() as conn:
        return {row["token_hash"] for row in conn.fetchall("SELECT token_hash FROM sessions")}


def assert_cookie_removed(response):
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f'{COOKIE_NAME}="";')
    for attribute in ("Max-Age=0", "Path=/", "HttpOnly", "Secure"):
        assert attribute in cookie
    assert "samesite=lax" in cookie.lower()


def test_logout_removes_only_current_session(office):
    client, me = office.login("Мария")
    token = client.cookies[COOKIE_NAME]
    add_session(office, me["id"], "second-session-of-maria")
    other, them = office.login("Ира")
    task_id = office.queued_task(me["id"])

    response = client.delete("/api/office-session")
    assert response.status_code == 204
    assert response.content == b""
    assert_cookie_removed(response)
    assert COOKIE_NAME not in client.cookies
    assert token_hash(token) not in session_hashes(office)

    stale = office.client()
    for method, path in PROTECTED:
        kwargs = {"json": BODIES[path]} if method == "post" else {}
        response = getattr(stale, method)(path, headers=with_token(token), **kwargs)
        assert (path, response.status_code, response.json()["code"]) == (path, 401, "unauthorized")
    assert client.get("/api/me").status_code == 401

    assert stale.get("/api/me", headers=with_token("second-session-of-maria")).json()["id"] == me["id"]
    assert other.get("/api/me").json()["id"] == them["id"]
    assert office.store.get_task(task_id).owner_id == me["id"]
    with office.store.db.connection() as conn:
        assert conn.fetchone("SELECT id FROM participants WHERE id = :id", {"id": me["id"]}) is not None


def test_logout_is_idempotent(office):
    client, _ = office.login()
    token = client.cookies[COOKIE_NAME]
    assert client.delete("/api/office-session").status_code == 204
    anonymous = office.client()
    for headers in ({}, with_token(token), with_token("unknown-token")):
        response = anonymous.delete("/api/office-session", headers=headers)
        assert response.status_code == 204
        assert_cookie_removed(response)


def test_logout_with_expired_session(office):
    client, _ = office.login()
    token = client.cookies[COOKIE_NAME]
    with office.store.db.transaction() as conn:
        conn.execute("UPDATE sessions SET expires_at = :past", {"past": PAST})
    response = client.delete("/api/office-session")
    assert response.status_code == 204
    assert_cookie_removed(response)
    assert token_hash(token) not in session_hashes(office)


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_logout_checks_origin(office, origin):
    client, me = office.login()
    response = client.delete("/api/office-session", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_origin"
    assert "set-cookie" not in response.headers
    assert client.get("/api/me").json()["id"] == me["id"]


@pytest.mark.parametrize("ending", ["logout", "expire"])
@pytest.mark.parametrize("stream", ["/api/events", "task"])
def test_stream_closes_after_session_ends(office, ending, stream):
    app = office.new_app(sse_ping_seconds=0.2)
    client, me = office.login(app=app)
    token = client.cookies[COOKIE_NAME]
    other, them = office.login("Ира", app=app)
    task_id = office.queued_task(me["id"])
    url = f"/api/tasks/{task_id}/events" if stream == "task" else stream
    pings = 0
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == ": ping"
        if ending == "logout":
            assert office.client(app).delete("/api/office-session", headers=with_token(token)).status_code == 204
        else:
            with office.store.db.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET expires_at = :past WHERE token_hash = :hash",
                    {"past": PAST, "hash": token_hash(token)},
                )
        for line in lines:
            if line == ": ping":
                pings += 1
                if pings > 5:
                    break
    assert pings <= 2
    assert other.get("/api/me").json()["id"] == them["id"]
