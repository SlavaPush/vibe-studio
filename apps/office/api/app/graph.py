import hashlib
import json
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.db import utcnow
from app.roles import (
    CRITIC,
    DEVELOPER,
    QA,
    QA_TEST_DIR,
    RESEARCH_NOTES,
    RESEARCH_PLAN,
    SUPERVISOR,
    Action,
    CriticReview,
    Entry,
    Finding,
    ModelFinding,
    QaTurn,
    ResearchNotes,
    ResearchPlan,
    SpecDraft,
    Turn,
    assemble,
)
from app.research import FetchError, fetch_public
from app.sandbox import SandboxError, StepDenied
from app.schemas import AgentRole, Task, TaskEventType, TaskStatus
from app.tools import ToolOutcome, WorkspaceTools

if TYPE_CHECKING:
    from app.runner import Runner

S = TaskStatus
GRAPH_STATUSES = frozenset({S.awaiting_spec_approval, S.developing, S.reviewing})
DEV_TURNS = 6
QA_TURNS = 3
QA_ATTEMPTS = 2
VERIFY_RETURNS = 2
MAX_FIX_CYCLES = 2
INFRA_RETRIES = 1
LISTING_LIMIT = 200
STEP_OUTPUT = 8 * 1024
DIFF_LIMIT = 200 * 1024
MAX_FETCHES = 3


class Stopped(Exception):
    pass


class State(TypedDict, total=False):
    task_id: str
    attempt: int
    draft: dict[str, Any] | None
    research: dict[str, Any] | None
    expected_version: int
    dev_round: int
    round_base_sha: str
    dev_sha: str
    verified: bool
    verify_failures: int
    check_summary: str
    check_feedback: str
    findings: list[dict[str, Any]]
    qa_findings: list[dict[str, Any]]
    critic_findings: list[dict[str, Any]]
    fix: bool


def spec_hash(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def commit_message(summary: str, role: AgentRole, marker: str) -> str:
    title = " ".join(summary.split())[:72] or f"Изменения роли {role.value}"
    return f"{title}\n\nOffice-Role: {role.value}\nOffice-Round: {marker}"


def describe(action: Action) -> str:
    target = action.step if action.kind == "run_step" else action.path
    return f"{action.kind} {target}"


class TaskGraph:
    def __init__(self, runner: "Runner", task_id: str, attempt: int, cancel: threading.Event) -> None:
        self.runner = runner
        self.store = runner.store
        self.workspace = runner.workspace
        self.sandbox = runner.sandbox
        self.gateway = runner.gateway
        self.settings = runner.settings
        self.task_id = task_id
        self.attempt = attempt
        self.cancel = cancel

    def compile(self, checkpointer: Any) -> Any:
        graph = StateGraph(State)
        for name in (
            "prepare", "specify", "research", "publish_spec", "await_spec", "start_dev_round",
            "develop", "verify", "to_review", "qa", "critic", "gate",
        ):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "specify")
        graph.add_conditional_edges(
            "specify", lambda state: "research" if (state.get("draft") or {}).get("needs_research") else "publish_spec"
        )
        graph.add_edge("research", "publish_spec")
        graph.add_edge("publish_spec", "await_spec")
        graph.add_edge("await_spec", "start_dev_round")
        graph.add_edge("start_dev_round", "develop")
        graph.add_edge("develop", "verify")
        graph.add_conditional_edges(
            "verify", lambda state: "to_review" if state.get("verified") else "start_dev_round"
        )
        graph.add_edge("to_review", "qa")
        graph.add_edge("to_review", "critic")
        graph.add_edge(["qa", "critic"], "gate")
        graph.add_conditional_edges("gate", lambda state: "start_dev_round" if state.get("fix") else END)
        return graph.compile(checkpointer=checkpointer)

    def guard(self, *statuses: TaskStatus) -> Task:
        if self.cancel.is_set():
            raise Stopped("cancelled")
        if self.store.attempt(self.task_id) != self.attempt:
            raise Stopped("attempt replaced")
        task = self.store.get_task(self.task_id)
        if task.status not in statuses:
            raise Stopped(f"status {task.status}")
        if task.status in (S.developing, S.reviewing):
            started = self.store.attempt_started_at(self.task_id)
            limit = timedelta(minutes=self.settings.task_time_limit_minutes)
            if started is not None and utcnow() - started > limit:
                self.halt(None, "time", "Превышен предел времени выполнения задачи.")
        return task

    def halt(self, role: AgentRole | None, step: str, reason: str, target: TaskStatus = S.needs_attention) -> None:
        self.store.halt(self.task_id, target, statuses=GRAPH_STATUSES, role=role, step=step, reason=reason)
        raise Stopped(reason)

    def started(self, role: AgentRole, step: str) -> None:
        self.store.add_event(self.task_id, TaskEventType.role_started, {"step": step}, role=role)

    def finished(self, role: AgentRole, step: str, outcome: str, summary: str) -> None:
        payload = {"step": step, "outcome": outcome, "summary": summary[:300]}
        self.store.add_event(self.task_id, TaskEventType.role_finished, payload, role=role)

    def ask(
        self,
        role: AgentRole,
        step: str,
        system: str,
        fixed: list[Entry],
        turns: list[list[Entry]],
        reply_type: type,
        status: TaskStatus,
    ) -> Any:
        task = self.guard(status)
        user = assemble(system, fixed, turns, self.gateway.input_budget(task.profile, role))
        reply = self.gateway.structured(
            self.store, self.task_id, task.profile, role, step, system, user, reply_type
        )
        self.guard(status)
        return reply

    def artifact(self, name: str, data: Any, role: AgentRole) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        directory = self.settings.artifacts_dir / self.task_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(body)
        self.store.add_event(
            self.task_id,
            TaskEventType.artifact_ready,
            {"name": name, "size_bytes": len(body)},
            role=role,
            artifact=name,
        )

    def listing(self) -> str:
        path = self.workspace.task_worktree(self.task_id)
        files = [
            name for name in self.workspace.tracked_files(path)
            if name.startswith("web/src/") and self.workspace.paths.readable(name)
        ]
        lines = files[:LISTING_LIMIT]
        if len(files) > LISTING_LIMIT:
            lines.append(f"... ещё {len(files) - LISTING_LIMIT} файлов")
        return "\n".join(lines)

    def spec_entries(self, task: Task) -> list[Entry]:
        assert task.spec is not None
        criteria = "\n".join(f"{index}. {item}" for index, item in enumerate(task.spec.acceptance_criteria, 1))
        return [
            Entry("Текст задачи участника", task.text, required=True),
            Entry("Требования", f"{task.spec.summary}\n\nКритерии приёмки:\n{criteria}", required=True),
        ]

    def log_tail(self, url: str) -> str:
        path = self.settings.artifacts_dir / self.task_id / url.rsplit("/", 1)[-1]
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-STEP_OUTPUT:].decode("utf-8", "replace")

    def run_step(self, step: str | None, role: AgentRole) -> ToolOutcome:
        try:
            result = self.sandbox.run_step(self.task_id, step or "", role)
        except StepDenied as error:
            return ToolOutcome("denied", str(error))
        except SandboxError as error:
            return ToolOutcome("error", str(error))
        tail = self.log_tail(result.artifact_urls[0]) if result.artifact_urls else ""
        return ToolOutcome("ok", f"Код выхода {result.exit_code}.\n{tail}")

    def execute(
        self, actions: list[Action], role: AgentRole, status: TaskStatus, only_path: str | None = None
    ) -> list[Entry]:
        tools = WorkspaceTools(self.store, self.workspace, self.task_id, role)
        entries = []
        for index, action in enumerate(actions, 1):
            self.guard(status)
            if only_path is not None and action.kind in ("write_file", "delete_file") and action.path != only_path:
                outcome = ToolOutcome("denied", f"QA записывает только файл {only_path}.")
                self.store.add_event(
                    self.task_id,
                    TaskEventType.tool_called,
                    {"arguments": (action.path or "")[:200], "result": "denied", "reason": outcome.output},
                    role=role,
                    tool=action.kind,
                )
            elif action.kind == "list_dir":
                outcome = tools.list_dir(action.path or ".")
            elif action.kind == "read_file":
                outcome = tools.read_file(action.path or "")
            elif action.kind == "write_file":
                outcome = tools.write_file(action.path or "", action.content or "")
            elif action.kind == "delete_file":
                outcome = tools.delete_file(action.path or "")
            else:
                outcome = self.run_step(action.step, role)
            entries.append(Entry(f"Результат действия {index}: {describe(action)}", f"{outcome.result}\n{outcome.output}"))
        return entries

    def turns(
        self,
        role: AgentRole,
        step: str,
        system: str,
        fixed: list[Entry],
        reply_type: type,
        limit: int,
        status: TaskStatus,
        only_path: str | None = None,
    ) -> Any:
        history: list[list[Entry]] = []
        reply = None
        for number in range(1, limit + 1):
            reply = self.ask(role, step, system, fixed, history, reply_type, status)
            entries = [Entry(f"Ход {number}: ответ роли", reply.summary)]
            entries += self.execute(reply.actions, role, status, only_path)
            history.append(entries)
            if reply.done:
                break
        return reply

    def prepare(self, state: State) -> State:
        self.guard(*GRAPH_STATUSES)
        self.workspace.ensure_worktree(self.task_id)
        task = self.guard(*GRAPH_STATUSES)
        if not task.base_sha:
            raise Stopped("worktree is not ready")
        return {}

    def specify(self, state: State) -> State:
        task = self.guard(S.awaiting_spec_approval, S.developing)
        if task.spec is not None:
            return {"draft": None}
        if task.status != S.awaiting_spec_approval:
            raise Stopped("developing without spec")
        self.started(AgentRole.supervisor, "spec")
        fixed = [
            Entry("Текст задачи участника", task.text, required=True),
            Entry("Файлы web/src", self.listing()),
        ]
        draft = self.ask(
            AgentRole.supervisor, "spec", SUPERVISOR, fixed, [], SpecDraft, S.awaiting_spec_approval
        )
        self.finished(
            AgentRole.supervisor, "spec", "ok", f"Требования: {len(draft.acceptance_criteria)} критериев приёмки."
        )
        return {"draft": draft.model_dump()}

    def research(self, state: State) -> State:
        task = self.guard(S.awaiting_spec_approval)
        draft = state["draft"] or {}
        questions = "\n".join(draft.get("research_questions") or []) or draft.get("summary", "")
        self.started(AgentRole.researcher, "research")
        base = [
            Entry("Текст задачи участника", task.text, required=True),
            Entry("Вопросы исследования", questions, required=True),
        ]
        plan = self.ask(
            AgentRole.researcher, "research_plan", RESEARCH_PLAN, base, [], ResearchPlan, S.awaiting_spec_approval
        )
        pages = []
        for url in plan.urls[:MAX_FETCHES]:
            self.guard(S.awaiting_spec_approval)
            try:
                page = fetch_public(url, **self.runner.fetch_options)
            except FetchError as error:
                payload = {"arguments": url[:200], "result": "denied", "reason": str(error)}
            else:
                pages.append(page)
                payload = {"arguments": url[:200], "result": "ok"}
            self.store.add_event(
                self.task_id, TaskEventType.tool_called, payload, role=AgentRole.researcher, tool="fetch_public"
            )
        if not pages:
            self.finished(AgentRole.researcher, "research", "blocked", "Публичные источники не загружены.")
            return {"research": {"notes": "", "sources": []}}
        fixed = base + [Entry(f"Страница {page.url} ({page.title})", page.text) for page in pages]
        notes = self.ask(
            AgentRole.researcher, "research_notes", RESEARCH_NOTES, fixed, [], ResearchNotes, S.awaiting_spec_approval
        )
        fetched = {page.url for page in pages}
        sources = [source.model_dump() for source in notes.sources if source.url in fetched]
        self.finished(AgentRole.researcher, "research", "ok", f"Источников: {len(sources)}.")
        return {"research": {"notes": notes.notes, "sources": sources}}

    def publish_spec(self, state: State) -> State:
        task = self.guard(S.awaiting_spec_approval, S.developing)
        published = self.store.published_version(self.task_id)
        if published is not None:
            return {"expected_version": published}
        if task.status != S.awaiting_spec_approval:
            approved = self.store.attempt_approval(self.task_id)
            if task.spec is None or approved is None:
                raise Stopped("spec is not published")
            return {"expected_version": approved}
        spec = None
        artifact = None
        if task.spec is None:
            draft = state.get("draft")
            if not draft:
                raise Stopped("no draft")
            research = state.get("research") or {}
            body = {
                "summary": draft["summary"],
                "acceptance_criteria": draft["acceptance_criteria"],
                "sources": research.get("sources", []),
            }
            spec = {"version": 1, "spec_hash": spec_hash(body)} | body
            name = "spec-1.json"
            data = json.dumps(spec | {"research_notes": research.get("notes", "")}, ensure_ascii=False, indent=2)
            directory = self.settings.artifacts_dir / self.task_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(data, encoding="utf-8")
            artifact = (name, len(data.encode("utf-8")))
        version = self.store.publish_spec(self.task_id, spec, artifact)
        if version is None:
            raise Stopped("task left awaiting_spec_approval")
        return {"expected_version": version}

    def await_spec(self, state: State) -> State:
        expected = state["expected_version"]
        while True:
            self.guard(S.awaiting_spec_approval, S.developing)
            if self.store.spec_approval(self.task_id, expected) is not None:
                return {}
            interrupt({"decision": "approve_spec", "expected_version": expected})

    def start_dev_round(self, state: State) -> State:
        return {
            "dev_round": state.get("dev_round", 0) + 1,
            "round_base_sha": self.workspace.head(self.task_id),
        }

    def develop(self, state: State) -> State:
        task = self.guard(S.developing)
        marker = f"{self.attempt}-{state['dev_round']}"
        head = self.workspace.head(self.task_id)
        if (
            head != state["round_base_sha"]
            and self.workspace.trailer(self.task_id, head, "Office-Role") == "developer"
            and self.workspace.trailer(self.task_id, head, "Office-Round") == marker
        ):
            return {"dev_sha": head, "check_feedback": ""}
        self.started(AgentRole.developer, "implement")
        fixed = self.spec_entries(task)
        findings = state.get("findings") or []
        if findings:
            fixed.append(Entry("Замечания QA и критика", json.dumps(findings, ensure_ascii=False, indent=1), required=True))
        if state.get("check_feedback"):
            fixed.append(Entry("Результат проверок прошлой версии", state["check_feedback"]))
        fixed.append(Entry("Файлы web/src", self.listing()))
        reply = self.turns(AgentRole.developer, "implement", DEVELOPER, fixed, Turn, DEV_TURNS, S.developing)
        self.guard(S.developing)
        sha = self.workspace.commit(
            self.task_id, commit_message(reply.summary, AgentRole.developer, marker), AgentRole.developer
        )
        if sha is None:
            self.halt(AgentRole.developer, "implement", "Разработчик не изменил файлы.")
        self.finished(AgentRole.developer, "implement", "ok", reply.summary or "Изменения зафиксированы.")
        return {"dev_sha": sha, "check_feedback": ""}

    def verify(self, state: State) -> State:
        self.guard(S.developing)
        report = self.sandbox.run_suite(self.task_id, state["dev_sha"], state["dev_sha"])
        self.guard(S.developing)
        summary = "\n".join(f"{check.command}: код выхода {check.exit_code}" for check in report.checks)
        failed = [check for check in report.checks if check.exit_code != 0]
        if not failed:
            return {"verified": True, "check_summary": summary}
        failures = state.get("verify_failures", 0) + 1
        if failures > VERIFY_RETURNS:
            self.halt(AgentRole.developer, "verify", "Проверки не проходят после двух возвратов разработчику.")
        tails = [f"{check.command}:\n{self.log_tail(check.artifact_urls[0])}" for check in failed if check.artifact_urls]
        feedback = summary + "\n\n" + "\n\n".join(tails)
        return {
            "verified": False,
            "verify_failures": failures,
            "check_summary": summary,
            "check_feedback": feedback[-2 * STEP_OUTPUT:],
        }

    def to_review(self, state: State) -> State:
        self.guard(S.developing, S.reviewing)
        if not self.store.ensure_status(self.task_id, S.developing, S.reviewing):
            raise Stopped("cannot enter reviewing")
        return {}

    def evidence(self, app_sha: str, qa_sha: str, test_file: str) -> Any:
        result = None
        for _ in range(1 + INFRA_RETRIES):
            self.guard(S.reviewing)
            report = self.sandbox.run_suite(self.task_id, app_sha, qa_sha, require_e2e=True)
            result = self.sandbox.e2e_result(self.task_id, report, test_file)
            if result.status != "no_evidence":
                return result
        self.halt(AgentRole.qa, "qa", f"Нет свидетельства QA: {result.reason}")

    def load_draft(self, path: Any) -> tuple[list[int], list[ModelFinding]] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data["criteria_covered"]), [ModelFinding.model_validate(item) for item in data["findings"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def qa(self, state: State) -> State:
        task = self.guard(S.reviewing)
        cycle = task.fix_cycles
        dev_sha = state["dev_sha"]
        marker = f"{self.attempt}-{state['dev_round']}"
        test_file = f"{QA_TEST_DIR}/{self.task_id}.pw.ts"
        self.started(AgentRole.qa, "qa")
        feedback = ""
        draft_path =self.settings.artifacts_dir / self.task_id / f"qa-draft-{marker}.json"
        for qa_try in range(1, QA_ATTEMPTS + 1):
            head = self.workspace.head(self.task_id)
            draft = None
            if (
                qa_try == 1
                and head != dev_sha
                and self.workspace.trailer(self.task_id, head, "Office-Role") == "qa"
                and self.workspace.trailer(self.task_id, head, "Office-Round") == marker
            ):
                draft = self.load_draft(draft_path)
            if draft is not None:
                qa_sha = head
                covered, model_findings = draft
            else:
                fixed = self.spec_entries(task) + [Entry("Файл теста QA", test_file, required=True)]
                if feedback:
                    fixed.append(Entry("Результат прошлой попытки", feedback, required=True))
                fixed.append(Entry("Файлы web/src", self.listing()))
                reply = self.turns(AgentRole.qa, "qa", QA, fixed, QaTurn, QA_TURNS, S.reviewing, test_file)
                covered = [item for item in reply.criteria_covered if 1 <= item <= len(task.spec.acceptance_criteria)]
                model_findings = list(reply.findings)
                self.guard(S.reviewing)
                draft_path.parent.mkdir(parents=True, exist_ok=True)
                draft_path.write_text(
                    json.dumps(
                        {"criteria_covered": covered, "findings": [item.model_dump() for item in model_findings]},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                sha = self.workspace.commit(
                    self.task_id, commit_message(reply.summary, AgentRole.qa, marker), AgentRole.qa
                )
                qa_sha = sha or self.workspace.head(self.task_id)
            if not (self.workspace.worktree_path(self.task_id) / test_file).is_file():
                feedback = f"Файл теста {test_file} не создан."
                if qa_try == QA_ATTEMPTS:
                    self.halt(AgentRole.qa, "qa", "QA не создал тест после двух попыток.")
                continue
            base = self.evidence(task.base_sha, qa_sha, test_file)
            if base.status == "passed":
                feedback = "Тест прошёл на исходной версии интерфейса. Он должен проверять новое поведение и не проходить без изменений разработчика."
                if qa_try == QA_ATTEMPTS:
                    self.halt(AgentRole.qa, "qa", "Тест QA проходит на исходной версии после двух попыток.")
                continue
            after = self.evidence(qa_sha, qa_sha, test_file)
            findings = [
                Finding(id=f"qa-{cycle}-{index}", source="qa", **item.model_dump())
                for index, item in enumerate(model_findings, 1)
            ]
            if after.status == "failed":
                findings.append(
                    Finding(
                        id=f"qa-{cycle}-e2e",
                        source="qa",
                        blocking=True,
                        criterion=covered[0] if covered else None,
                        file=test_file,
                        line=after.line,
                        text=after.message,
                    )
                )
            data = [item.model_dump() for item in findings]
            self.artifact(f"review-{cycle}-qa.json", {"qa_sha": qa_sha, "findings": data}, AgentRole.qa)
            blocking = any(item.blocking for item in findings)
            summary = "Тест QA не проходит на версии задачи." if after.status == "failed" else "Тест QA проходит на версии задачи и не проходит на исходной."
            self.finished(AgentRole.qa, "qa", "blocked" if blocking else "ok", summary)
            return {"qa_findings": data}
        raise Stopped("qa attempts exhausted")

    def critic(self, state: State) -> State:
        task = self.guard(S.reviewing)
        cycle = task.fix_cycles
        self.started(AgentRole.critic, "review")
        diff = self.workspace.diff_range(self.task_id, task.base_sha, state["dev_sha"], DIFF_LIMIT)
        fixed = self.spec_entries(task) + [
            Entry("Отчёт проверок", state.get("check_summary", ""), required=True),
            Entry("Diff изменений", diff),
        ]
        review = self.ask(AgentRole.critic, "review", CRITIC, fixed, [], CriticReview, S.reviewing)
        findings = [
            Finding(id=f"critic-{cycle}-{index}", source="critic", **item.model_dump()).model_dump()
            for index, item in enumerate(review.findings, 1)
        ]
        self.artifact(f"review-{cycle}-critic.json", {"verdict": review.verdict, "findings": findings}, AgentRole.critic)
        blocking = any(item["blocking"] for item in findings)
        self.finished(
            AgentRole.critic, "review", "blocked" if blocking else "ok", f"Вердикт {review.verdict}, замечаний: {len(findings)}."
        )
        return {"critic_findings": findings}

    def gate(self, state: State) -> State:
        task = self.guard(S.reviewing, S.developing, S.awaiting_integration)
        findings = (state.get("qa_findings") or []) + (state.get("critic_findings") or [])
        if not any(item["blocking"] for item in findings):
            if not self.store.ensure_status(self.task_id, S.reviewing, S.awaiting_integration):
                raise Stopped("cannot enter awaiting_integration")
            return {"fix": False, "findings": findings}
        if task.status == S.reviewing and task.fix_cycles >= MAX_FIX_CYCLES:
            self.halt(AgentRole.critic, "review", "Блокирующие замечания остались после двух циклов исправления.")
        cycles = task.fix_cycles + 1 if task.status == S.reviewing else task.fix_cycles
        if not self.store.ensure_status(self.task_id, S.reviewing, S.developing, fix_cycles=cycles):
            raise Stopped("cannot return to developing")
        return {"fix": True, "findings": findings, "verify_failures": 0, "verified": False}
