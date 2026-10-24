import os

import pytest

from app.artifacts import SECURITY_HEADERS
from app.schemas import CheckReport, CheckResult


@pytest.fixture
def task(office):
    client, me = office.login()
    return client, office.queued_task(me["id"])


def test_empty_report_has_nulls(office, task):
    client, task_id = task
    response = client.get(f"/api/tasks/{task_id}/checks")
    assert response.status_code == 200
    assert response.json() == {
        "task_id": task_id,
        "commit_sha": None,
        "spec_hash": None,
        "suite_hash": None,
        "checks": [],
    }
    office.set_task(task_id, base_sha="b" * 40)
    assert client.get(f"/api/tasks/{task_id}/checks").json()["commit_sha"] == "b" * 40


def test_filled_report_is_returned(office, task):
    client, task_id = task
    report = CheckReport(
        task_id=task_id,
        commit_sha="e" * 40,
        spec_hash="sha256:spec",
        suite_hash="sha256:suite",
        checks=[
            CheckResult(
                command="npm run build",
                exit_code=0,
                duration_ms=1200,
                artifact_urls=[f"/api/tasks/{task_id}/artifacts/build.log"],
            )
        ],
    )
    office.store.add_check_report(report)
    assert CheckReport.model_validate(client.get(f"/api/tasks/{task_id}/checks").json()) == report


def test_checks_of_missing_task(office, task):
    client, _ = task
    assert client.get("/api/tasks/tsk_missing/checks").status_code == 404


def write_artifact(office, task_id, name, content=b"data"):
    directory = office.settings.artifacts_dir / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(content)
    return directory / name


@pytest.mark.parametrize(
    ("name", "content", "media_type"),
    [
        ("report.html", b"<script>alert(1)</script>", "text/html"),
        ("diagram.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        ("shot.png", b"\x89PNG\r\n", "image/png"),
        ("build.log", b"ok", "text/plain"),
    ],
)
def test_artifact_has_security_headers(office, task, name, content, media_type):
    client, task_id = task
    write_artifact(office, task_id, name, content)
    response = client.get(f"/api/tasks/{task_id}/artifacts/{name}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith(media_type)
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value
    assert "content-disposition" not in response.headers


def test_archive_is_attachment(office, task):
    client, task_id = task
    write_artifact(office, task_id, "e2e-trace.zip", b"PK")
    response = client.get(f"/api/tasks/{task_id}/artifacts/e2e-trace.zip")
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name", ["..%2Fsecret.log", "sub%2Fbuild.log", "%2E%2E", ".hidden", "a..b.log"])
def test_invalid_artifact_name(office, task, name):
    client, task_id = task
    response = client.get(f"/api/tasks/{task_id}/artifacts/{name}")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_artifact_name"


def test_artifact_of_other_task_is_not_found(office, task):
    client, task_id = task
    _, other = office.login("Иван")
    other_id = office.queued_task(other["id"])
    write_artifact(office, other_id, "build.log")
    response = client.get(f"/api/tasks/{task_id}/artifacts/build.log")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("target", ["other_task", "outside"])
def test_symlinked_task_directory_is_rejected(office, task, tmp_path, target):
    client, task_id = task
    if target == "other_task":
        _, other = office.login("Иван")
        source = write_artifact(office, office.queued_task(other["id"]), "build.log").parent
    else:
        source = tmp_path / "outside"
        source.mkdir()
        (source / "build.log").write_text("secret")
    office.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(source, office.settings.artifacts_dir / task_id)
    response = client.get(f"/api/tasks/{task_id}/artifacts/build.log")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_symlink_artifact_is_rejected(office, task, tmp_path):
    client, task_id = task
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    directory = office.settings.artifacts_dir / task_id
    directory.mkdir(parents=True, exist_ok=True)
    os.symlink(secret, directory / "link.log")
    response = client.get(f"/api/tasks/{task_id}/artifacts/link.log")
    assert response.status_code == 404
