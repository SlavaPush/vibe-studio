from tests.conftest import client_for, login, make_app

LOGOUT = "/auth/logout"


def _empty_body(response) -> bool:
    if not response.content:
        return True
    try:
        return response.json() in ({}, None)
    except ValueError:
        return False


def test_logout_returns_success_without_body(db):
    app = make_app(db)
    with client_for(app) as client:
        user = login(client)
        assert user.get("/api/conversations").status_code == 200
        response = user.post(LOGOUT)
        assert response.status_code in (200, 204), response.text
        assert _empty_body(response), response.text


def test_old_session_rejected_after_logout(db):
    app = make_app(db)
    with client_for(app) as client:
        user = login(client)
        assert user.get("/api/conversations").status_code == 200
        response = user.post(LOGOUT)
        assert response.status_code in (200, 204), response.text
        after = user.get("/api/conversations")
        assert after.status_code == 401, after.text
        me = user.get("/api/me")
        assert me.status_code == 401, me.text


def test_logout_without_auth_returns_401(db):
    app = make_app(db)
    with client_for(app) as client:
        response = client.post(LOGOUT)
        assert response.status_code == 401, response.text
        bad = client.post(LOGOUT, headers={"Cookie": "vc_session=bogus", "X-CSRF-Token": "x"})
        assert bad.status_code == 401, bad.text
