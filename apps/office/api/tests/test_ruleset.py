import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
RULESET = ROOT / ".github" / "rulesets" / "main.json"
WORKFLOWS = ROOT / ".github" / "workflows"
GITHUB_ACTIONS = 15368
ADMIN_ROLE = 5


def load_ruleset() -> dict:
    return json.loads(RULESET.read_text(encoding="utf-8"))


def rule(ruleset: dict, kind: str) -> dict:
    matches = [item for item in ruleset["rules"] if item["type"] == kind]
    assert len(matches) == 1, kind
    return matches[0].get("parameters", {})


def workflow_jobs() -> set[str]:
    jobs: set[str] = set()
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files
    for path in files:
        jobs.update(yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"])
    return jobs


def test_ruleset_targets_default_branch():
    ruleset = load_ruleset()
    assert ruleset["target"] == "branch"
    assert ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    rule(ruleset, "deletion")
    rule(ruleset, "non_fast_forward")


def test_required_checks_match_workflow_jobs():
    checks = rule(load_ruleset(), "required_status_checks")
    contexts = [item["context"] for item in checks["required_status_checks"]]
    assert len(contexts) == len(set(contexts))
    assert set(contexts) == workflow_jobs()
    assert all(item.get("integration_id") == GITHUB_ACTIONS for item in checks["required_status_checks"])
    assert checks["strict_required_status_checks_policy"] is False


def test_only_repository_admin_bypasses():
    actors = load_ruleset()["bypass_actors"]
    assert actors == [{"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    assert all(actor["actor_type"] != "Integration" for actor in actors)


def test_pull_request_requires_code_owner_review():
    params = rule(load_ruleset(), "pull_request")
    assert params["required_approving_review_count"] == 0
    assert params["require_code_owner_review"] is True
    assert params["dismiss_stale_reviews_on_push"] is True
