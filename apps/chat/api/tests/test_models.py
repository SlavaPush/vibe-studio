from fastapi.testclient import TestClient

from app.main import app

EXPECTED_IDS = [
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "gpt-6-luna",
    "gpt-6.1-sol",
    "gpt-image-2.5-flare",
]


def test_models_returns_catalog_without_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    response = TestClient(app).get("/api/models")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == EXPECTED_IDS
    assert all(set(item) == {"id", "provider", "label", "kind", "selectable"} for item in items)
    assert "secret" not in response.text
    assert "sk-" not in response.text
    assert [item["id"] for item in items if item["kind"] == "image"] == ["gpt-image-2.5-flare"]
    assert [item["id"] for item in items if not item["selectable"]] == ["gpt-image-2.5-flare"]
