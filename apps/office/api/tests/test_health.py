def test_health_reports_ok(office):
    response = office.client().get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
