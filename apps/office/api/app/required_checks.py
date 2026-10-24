import json
from dataclasses import dataclass
from pathlib import Path

import yaml

PR_EVENTS = ("pull_request", "pull_request_target")


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    workflow: str
    pr_event: str
    push_event: str | None


def _events(workflow: dict) -> list[str]:
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, str):
        return [triggers]
    return list(triggers or [])


def load_required_checks(root: Path) -> list[RequiredCheck]:
    ruleset = json.loads((root / ".github" / "rulesets" / "main.json").read_text(encoding="utf-8"))
    names = [
        check["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]
    jobs: dict[str, RequiredCheck] = {}
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        events = _events(workflow)
        pr_event = next((event for event in PR_EVENTS if event in events), None)
        if pr_event is None:
            continue
        push_event = "push" if "push" in events else None
        for job_id, job in workflow.get("jobs", {}).items():
            name = job.get("name", job_id)
            jobs[name] = RequiredCheck(name, f".github/workflows/{path.name}", pr_event, push_event)
    missing = [name for name in names if name not in jobs]
    if missing or not names:
        raise ValueError(f"Обязательные проверки без workflow pull request: {', '.join(missing) or 'список пуст'}")
    return [jobs[name] for name in names]
