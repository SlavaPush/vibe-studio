import json
import re
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app import schemas
from app.errors import ErrorCode
from conftest import CONTRACT_CODES, classification

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "api.md"
EXAMPLE = re.compile(r"<!-- example: (\S+) -->\s*```json\n(.*?)\n```", re.DOTALL)

PLAN_STATUSES = [
    "rejected",
    "queued",
    "awaiting_spec_approval",
    "developing",
    "reviewing",
    "awaiting_integration",
    "awaiting_release",
    "deploying",
    "released",
    "failed",
    "cancelled",
    "needs_attention",
]

PLAN_EVENT_TYPES = [
    "task_created",
    "task_rejected",
    "status_changed",
    "role_started",
    "role_finished",
    "tool_called",
    "artifact_ready",
    "decision_required",
    "decision_recorded",
    "check_result",
    "release_candidate",
    "deploy_status",
    "pr_opened",
    "checks_status",
    "pr_closed",
    "operation_unknown",
]


def load_examples() -> list[tuple[str, object]]:
    text = CONTRACT.read_text(encoding="utf-8")
    return [(name, json.loads(body)) for name, body in EXAMPLE.findall(text)]


def adapter(name: str) -> TypeAdapter:
    if name.endswith("[]"):
        return TypeAdapter(list[getattr(schemas, name[:-2])])
    return TypeAdapter(getattr(schemas, name))


def examples_of(name: str) -> list[object]:
    return [value for example_name, value in load_examples() if example_name == name]


def test_every_marker_has_json_example():
    markers = re.findall(r"^<!-- example:", CONTRACT.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(load_examples()) == len(markers) >= 20


@pytest.mark.parametrize(("name", "value"), load_examples())
def test_example_matches_schema(name, value):
    adapter(name).validate_python(value)


def test_statuses_match_plan_and_contract():
    assert [status.value for status in schemas.TaskStatus] == PLAN_STATUSES
    assert examples_of("TaskStatus[]") == [PLAN_STATUSES]


def test_event_types_match_plan_and_contract():
    assert [event_type.value for event_type in schemas.TaskEventType] == PLAN_EVENT_TYPES
    assert examples_of("TaskEventType[]") == [PLAN_EVENT_TYPES]


def test_event_examples_cover_every_type():
    events = examples_of("TaskEvent") + [event for batch in examples_of("TaskEvent[]") for event in batch]
    assert {event["type"] for event in events} == set(PLAN_EVENT_TYPES)


def test_error_examples_cover_access_and_version_errors():
    codes = {error["code"] for error in examples_of("ApiError")}
    assert {"forbidden_not_owner", "forbidden_role", "version_mismatch", "unauthorized"} <= codes


def test_unknown_fields_are_rejected():
    request = examples_of("TaskDecisionRequest")[0] | {"role": "admin"}
    with pytest.raises(ValueError):
        schemas.TaskDecisionRequest.model_validate(request)


def test_error_codes_match_contract_table():
    assert set(CONTRACT_CODES) == {code.value for code in ErrorCode} | {"diff_too_large"}
    assert CONTRACT_CODES["forbidden_origin"] == 403


def test_every_route_is_documented(office):
    text = CONTRACT.read_text(encoding="utf-8")
    headings = "\n".join(re.findall(r"^### .*$", text, re.MULTILINE))
    documented = {
        re.sub(r"\{[^}]+\}", "{}", operation)
        for operation in re.findall(r"`((?:GET|POST|PUT|PATCH|DELETE) /api/[^`]*)`", headings)
    }
    routes = {
        f"{method} {re.sub(r'{[^}]+}', '{}', route.path)}"
        for route in office.app.routes
        if route.path.startswith("/api/") and route.path != "/api/health"
        for method in getattr(route, "methods", ())
        if method != "HEAD"
    }
    assert routes == documented
    assert "DELETE /api/office-session" in routes


def test_check_report_example_allows_missing_hashes():
    reports = [schemas.CheckReport.model_validate(value) for value in examples_of("CheckReport")]
    assert any(report.commit_sha is None and report.spec_hash is None for report in reports)


def test_routes_follow_schemas(office):
    client, me = office.login("Вячеслав", admin=True)
    assert schemas.Participant.model_validate(client.get("/api/me").json()).id == me["id"]

    created = office.post_task(client)
    assert created.status_code == 201
    office.anthropic.answer = classification(False, "out_of_scope")
    office.post_task(client, "Перевести backend vibe-chat на другую базу данных PostgreSQL.")
    office.add_candidate(created.json()["id"])

    tasks = TypeAdapter(list[schemas.Task]).validate_python(client.get("/api/tasks").json())
    assert [task.status for task in tasks] == [
        schemas.TaskStatus.rejected,
        schemas.TaskStatus.awaiting_spec_approval,
    ]
    task = schemas.Task.model_validate(client.get(f"/api/tasks/{tasks[1].id}").json())
    assert task.owner_name == "Вячеслав"
    schemas.Slots.model_validate(client.get("/api/slots").json())
    schemas.CheckReport.model_validate(client.get(f"/api/tasks/{task.id}/checks").json())
    releases = TypeAdapter(list[schemas.ReleaseCandidate]).validate_python(
        client.get("/api/releases").json()
    )
    assert [release.status for release in releases] == [schemas.ReleaseStatus.pending]


def test_unknown_task_returns_api_error(office):
    client, _ = office.login()
    response = client.get("/api/tasks/tsk_missing")
    assert response.status_code == 404
    error = schemas.ApiError.model_validate(response.json())
    assert error.code == "not_found"
    assert error.retryable is False
