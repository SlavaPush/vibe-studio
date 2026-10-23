from fastapi.testclient import TestClient

from app.main import app


def test_version_returns_build_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc123")
    response = TestClient(app).get("/api/version")
    assert response.status_code == 200
    assert response.json() == {"version": "abc123"}
