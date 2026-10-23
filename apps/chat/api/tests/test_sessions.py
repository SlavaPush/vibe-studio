from conftest import DEMO_CODE, client_for, login, make_app, sql


def test_demo_session_sets_secure_cookie_and_me_returns_session(db):
    with client_for(make_app(db)) as client:
        response = client.post("/api/demo-session", json={"code": DEMO_CODE})
        assert response.status_code == 201
        cookie = response.headers["set-cookie"]
        assert "vc_session=" in cookie
        for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert flag.lower() in cookie.lower()
        body = response.json()
        token = response.cookies.get("vc_session")
        client.cookies.clear()

        me = client.get("/api/me", headers={"Cookie": f"vc_session={token}"})
        assert me.status_code == 200
        assert me.json() == body
        stored = sql(db, "SELECT token_hash FROM sessions")
        assert stored and stored[0][0] != token


def test_wrong_code_origin_and_missing_session(db):
    with client_for(make_app(db)) as client:
        wrong = client.post("/api/demo-session", json={"code": "wrong"})
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "invalid_demo_code"

        foreign = client.post("/api/demo-session", json={"code": DEMO_CODE}, headers={"Origin": "https://evil.test"})
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "csrf_failed"

        anonymous = client.get("/api/me")
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "unauthorized"
        assert set(anonymous.json()) == {"code", "message", "retryable", "operation_id"}

        extra = client.post("/api/demo-session", json={"code": DEMO_CODE, "admin": True})
        assert extra.status_code == 422
        assert extra.json()["code"] == "validation_error"


def test_mutation_requires_csrf_token_and_origin(db):
    with client_for(make_app(db)) as client:
        user = login(client)
        cookie = {"Cookie": f"vc_session={user.token}"}
        missing = client.post("/api/conversations", json={"title": None}, headers=cookie)
        assert missing.status_code == 403
        assert missing.json()["code"] == "csrf_failed"

        foreign = client.post(
            "/api/conversations",
            json={"title": None},
            headers={**cookie, "X-CSRF-Token": user.csrf, "Origin": "https://evil.test"},
        )
        assert foreign.status_code == 403

        ok = client.post("/api/conversations", json={"title": None}, headers={**cookie, "X-CSRF-Token": user.csrf})
        assert ok.status_code == 201


def test_expired_session_is_rejected(db):
    with client_for(make_app(db)) as client:
        user = login(client)
        sql(db, "UPDATE sessions SET expires_at = now() - interval '1 minute'")
        assert user.get("/api/me").status_code == 401


def test_session_creation_is_rate_limited_per_source(db):
    with client_for(make_app(db, sessions_per_hour=2)) as client:
        assert client.post("/api/demo-session", json={"code": DEMO_CODE}).status_code == 201
        assert client.post("/api/demo-session", json={"code": DEMO_CODE}).status_code == 201
        limited = client.post("/api/demo-session", json={"code": DEMO_CODE})
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"
        assert limited.json()["retryable"] is True


def test_version_is_not_cached_and_health_is_ready(db):
    with client_for(make_app(db)) as client:
        assert client.get("/api/version").headers["cache-control"] == "no-store"
        assert client.get("/healthz").json() == {"status": "ok"}
