def test_unknown_field_is_validation_error(office):
    client, _ = office.login()
    response = client.post("/api/tasks", json={"text": "x" * 30, "role": "admin"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["retryable"] is False


def test_text_length_is_validated(office):
    client, _ = office.login()
    for text in ("коротко", "я" * 4001):
        response = client.post("/api/tasks", json={"text": text})
        assert response.status_code == 422
        assert response.json()["message"] == "Текст задачи должен содержать от 20 до 4000 символов."


def test_unknown_route_is_not_found(office):
    client, _ = office.login()
    response = client.get("/api/unknown")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_internal_error_hides_details(office, monkeypatch):
    client, _ = office.login()

    def broken():
        raise RuntimeError("secret internal path /Users/office")

    monkeypatch.setattr(office.store, "list_tasks", broken)
    cookie = {"Cookie": f"office_session={client.cookies['office_session']}"}
    response = office.client(raise_server_exceptions=False).get("/api/tasks", headers=cookie)
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "secret" not in body["message"]
    assert body["operation_id"].startswith("op_")
