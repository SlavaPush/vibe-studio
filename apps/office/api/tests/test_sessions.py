import json
import threading
from datetime import datetime, timedelta

import pytest

from app.access_codes import AccessCodes, access_code_hash
from app.config import Settings
from app.db import Database, to_text, utcnow
from app.errors import OfficeError
from app.sessions import COOKIE_NAME, token_hash
from conftest import CODE_VECTORS

pytestmark = pytest.mark.pg

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


def login_with(client, code):
    return client.post("/api/office-session", json={"access_code": code})


@pytest.mark.parametrize("role", ["participant", "admin"])
def test_login_sets_cookie_and_stores_only_hash(office, role):
    client = office.client()
    response = login_with(client, office.add_code("Мария", role))
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == role
    assert body["display_name"] == "Мария"
    assert body["id"].startswith("prt_")
    lifetime = datetime.fromisoformat(body["expires_at"]) - utcnow()
    assert timedelta(hours=167) < lifetime <= timedelta(hours=168)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    token = client.cookies[COOKIE_NAME]
    with office.store.db.connection() as conn:
        rows = conn.fetchall("SELECT token_hash FROM sessions")
    assert [row["token_hash"] for row in rows] == [token_hash(token)]
    assert token not in str(rows)
    assert client.get("/api/me").json()["id"] == body["id"]
    row = office.code_row("Мария")
    assert row["office_used_at"] is not None
    assert row["used_at"] is None


def test_wrong_code_and_rate_limit(office):
    code = office.add_code("Мария")
    client = office.client()
    for _ in range(5):
        response = login_with(client, "wrong")
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_access_code"
    response = login_with(client, code)
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert response.json()["retryable"] is True
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert office.code_row("Мария")["office_used_at"] is None


def test_parallel_wrong_codes_respect_limit(office):
    client = office.client()
    for _ in range(4):
        login_with(client, "wrong")
    barrier = threading.Barrier(6)
    statuses = []

    def attempt():
        barrier.wait()
        response = login_with(client, "wrong")
        statuses.append((response.status_code, response.json()["code"]))

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [(401, "invalid_access_code")] + [(429, "rate_limited")] * 5


def test_login_request_accepts_only_access_code(office):
    client = office.client()
    code = office.add_code("Мария")
    assert login_with(client, "").status_code == 422
    response = client.post("/api/office-session", json={"access_code": code, "display_name": "Другое имя"})
    assert response.status_code == 422
    assert office.code_row("Мария")["office_used_at"] is None


def test_code_enters_office_once_in_any_format(office):
    code = office.add_code("Мария")
    client = office.client()
    first = login_with(client, code)
    assert first.status_code == 200
    for variant in (code, code.upper().replace("-", " "), f" {code.replace('-', '')}\t"):
        response = login_with(office.client(), variant)
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_access_code"
    assert client.get("/api/me").json()["id"] == first.json()["id"]


def test_parallel_logins_with_one_code_give_one_session(office):
    app = office.new_app(login_failures_per_minute=100)
    code = office.add_code("Мария")
    barrier = threading.Barrier(8)
    statuses = []

    def attempt():
        barrier.wait()
        statuses.append(login_with(office.client(app), code).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200] + [401] * 7
    with office.store.db.connection() as conn:
        assert conn.fetchone("SELECT count(*) AS n FROM sessions")["n"] == 1


def test_parallel_claims_from_independent_registries_give_one_code(office, codes_url):
    code = office.add_code("Мария")
    registries = [AccessCodes(Settings(codes_database_url=codes_url)) for _ in range(6)]
    barrier = threading.Barrier(len(registries))
    results = []

    def attempt(registry):
        barrier.wait()
        results.append(registry.claim(code))

    threads = [threading.Thread(target=attempt, args=(registry,)) for registry in registries]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        for registry in registries:
            registry.close()
    claimed = [result for result in results if result is not None]
    assert len(results) == len(registries)
    assert [(row["label"], row["office_role"]) for row in claimed] == [("Мария", "participant")]


def test_chat_login_does_not_spend_office_login(office):
    code = office.add_code("Мария")
    office.update_code("Мария", "used_at = CURRENT_TIMESTAMP")
    used_at = office.code_row("Мария")["used_at"]
    assert login_with(office.client(), code).status_code == 200
    assert login_with(office.client(), code).status_code == 401
    row = office.code_row("Мария")
    assert row["used_at"] == used_at
    assert row["office_used_at"] is not None


def test_code_without_office_role_is_rejected_and_not_spent(office):
    code = office.add_code("Без роли", None)
    response = login_with(office.client(), code)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_access_code"
    assert office.code_row("Без роли")["office_used_at"] is None
    office.update_code("Без роли", "office_role = 'participant'")
    response = login_with(office.client(), code)
    assert response.status_code == 200
    assert response.json()["role"] == "participant"


def test_revoked_code_cannot_enter(office):
    code = office.add_code("Мария")
    office.update_code("Мария", "revoked_at = CURRENT_TIMESTAMP")
    response = login_with(office.client(), code)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_access_code"
    assert office.code_row("Мария")["office_used_at"] is None


def test_revoking_code_closes_its_sessions_only(office):
    client, me = office.login("Мария")
    other, them = office.login("Ира")
    task_id = office.queued_task(me["id"])
    office.update_code("Мария", "revoked_at = CURRENT_TIMESTAMP")
    for method, path in PROTECTED:
        kwargs = {"json": BODIES[path]} if method == "post" else {}
        response = getattr(client, method)(path.replace("tsk_x", task_id), **kwargs)
        assert (path, response.status_code, response.json()["code"]) == (path, 401, "unauthorized")
    assert other.get("/api/me").json()["id"] == them["id"]


@pytest.mark.parametrize("stream", ["/api/events", "task"])
def test_stream_closes_after_code_is_revoked(office, stream):
    app = office.new_app(sse_ping_seconds=0.2)
    client, me = office.login(app=app)
    other, them = office.login("Ира", app=app)
    task_id = office.queued_task(me["id"])
    url = f"/api/tasks/{task_id}/events" if stream == "task" else stream
    pings = 0
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == ": ping"
        office.update_code("Мария", "revoked_at = CURRENT_TIMESTAMP")
        for line in lines:
            if line == ": ping":
                pings += 1
                if pings > 5:
                    break
    assert pings <= 2
    assert other.get("/api/me").json()["id"] == them["id"]


def test_stream_closes_when_registry_is_unavailable(office, tmp_path):
    app = office.new_app(sse_ping_seconds=0.2)
    client, _ = office.login(app=app)
    codes = app.state.sessions.codes
    pings = 0
    with client.stream("GET", "/api/events") as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == ": ping"
        working, codes.db = codes.db, Database(f"sqlite:///{tmp_path / 'missing' / 'codes.sqlite'}")
        for line in lines:
            if line == ": ping":
                pings += 1
                if pings > 5:
                    break
    codes.db = working
    assert pings <= 2


def test_login_and_session_fail_closed_without_registry(office, tmp_path):
    client, _ = office.login("Мария")
    token = client.cookies[COOKIE_NAME]
    code = office.add_code("Ира")
    missing = f"sqlite:///{tmp_path / 'missing' / 'codes.sqlite'}"
    for url in ("", missing):
        app = office.new_app(codes_database_url=url)
        anonymous = office.client(app)
        for _ in range(7):
            response = login_with(anonymous, code)
            assert response.status_code == 503
            assert response.json()["code"] == "codes_unavailable"
            assert response.json()["retryable"] is True
        session = office.client(app, cookies={COOKIE_NAME: token})
        assert session.get("/api/me").json()["code"] == "codes_unavailable"
        assert office.post_task(session).status_code == 503
    assert office.code_row("Ира")["office_used_at"] is None
    assert office.store.list_tasks() == []
    assert office.openai.moderation_calls == []
    assert login_with(office.client(), code).status_code == 200


def test_session_without_code_is_rejected(office):
    now = utcnow()
    with office.store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO participants (id, display_name, role, created_at) VALUES ('prt_old', 'Мария', 'admin', :now)",
            {"now": to_text(now)},
        )
    add_session(office, "prt_old", "session-before-p56")
    response = office.client().get("/api/me", headers=with_token("session-before-p56"))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_task_of_participant_before_p56_is_transferred_to_new_participant(office):
    with office.store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO participants (id, display_name, role, created_at) VALUES ('prt_old', 'Мария', 'participant', :now)",
            {"now": to_text(utcnow())},
        )
    task_id = office.queued_task("prt_old")
    office.store.promote_queue()
    client, me = office.login("Мария")
    body = {"decision": "cancel", "expected_version": office.store.get_task(task_id).version, "idempotency_key": "move-key-1"}
    assert client.post(f"/api/tasks/{task_id}/decisions", json=body).status_code == 403
    with office.store.db.transaction() as conn:
        conn.execute("UPDATE tasks SET owner_id = :owner WHERE id = :id", {"owner": me["id"], "id": task_id})
    body["idempotency_key"] = "move-key-2"
    assert client.post(f"/api/tasks/{task_id}/decisions", json=body).status_code == 200
    assert office.store.get_task(task_id).status == "cancelled"


def test_long_labels_are_kept_in_full(office):
    prefix = "Участник с очень длинной меткой кода доступа"
    assert len(prefix) > 40
    first, me = office.login(f"{prefix} 1")
    second, other = office.login(f"{prefix} 2")
    assert (me["display_name"], other["display_name"]) == (f"{prefix} 1", f"{prefix} 2")
    assert first.get("/api/me").json()["display_name"] == f"{prefix} 1"
    task = office.post_task(first).json()
    assert task["owner_name"] == f"{prefix} 1"


def test_hash_matches_chat_vectors():
    vectors = json.loads(CODE_VECTORS.read_text(encoding="utf-8"))
    assert len(vectors) >= 8
    for vector in vectors:
        assert access_code_hash(vector["code"]) == vector["hash"], vector["code"]


def test_active_check_is_cached_and_fails_closed_after_expiry(office, codes_url, tmp_path):
    now = [100.0]
    codes = AccessCodes(Settings(codes_database_url=codes_url, codes_check_seconds=10), clock=lambda: now[0])
    try:
        office.add_code("Мария")
        office.add_code("Ира")
        maria = office.code_row("Мария")["id"]
        ira = office.code_row("Ира")["id"]
        assert codes.active(maria) and codes.active(ira)
        office.update_code("Мария", "revoked_at = CURRENT_TIMESTAMP")
        now[0] += 9
        assert codes.active(maria)
        now[0] += 2
        assert not codes.active(maria)
        assert codes.active(ira)
        working, codes.db = codes.db, Database(f"sqlite:///{tmp_path / 'missing' / 'codes.sqlite'}")
        assert codes.active(ira)
        now[0] += 11
        with pytest.raises(OfficeError) as error:
            codes.active(ira)
        assert (error.value.status, error.value.code) == (503, "codes_unavailable")
        codes.db = working
    finally:
        codes.close()


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
    assert login_with(client, office.add_code("Ира")).status_code == 200


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


def test_relogin_with_other_code_replaces_current_session(office):
    client, me = office.login("Мария")
    old_token = client.cookies[COOKIE_NAME]
    response = login_with(client, office.add_code("Мария, администратор", "admin"))
    assert response.status_code == 200
    assert response.json()["id"] != me["id"]
    assert response.json()["role"] == "admin"
    assert response.json()["display_name"] == "Мария, администратор"
    assert client.cookies[COOKIE_NAME] != old_token
    assert token_hash(old_token) not in session_hashes(office)
    stale = office.client()
    assert stale.get("/api/me", headers={"Cookie": f"{COOKIE_NAME}={old_token}"}).status_code == 401


def test_logout_spends_office_login(office):
    code = office.add_code("Мария")
    client = office.client()
    assert login_with(client, code).status_code == 200
    assert client.delete("/api/office-session").status_code == 204
    response = login_with(client, code)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_access_code"


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
