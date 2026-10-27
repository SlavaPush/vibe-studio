import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.github import GitHubClient
from app.render import RenderClient
from app.schemas import ReleaseCandidate, ReleaseKind, ReleaseStatus, Task, TaskEventType
from app.tasks import TaskStore, candidate_from_row

SOURCE_LIMIT = 4096
LOG_PAGES = 10
DIFF_READ = 64 * 1024
HIDDEN = "[скрыто]"
UNAVAILABLE = "логи недоступны"
LOG_LEVELS = frozenset({"warning", "error", "critical", "fatal"})
SOURCES = {
    "verify_failed": "проверка после деплоя не пройдена, выполнен автоматический откат",
    "rolled_back": "выпуск откатан администратором",
    "checks_failed": "обязательные проверки или критический E2E итогового commit не пройдены",
}
ACCESS_LINE = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^"]*" \d{3}')
PATTERNS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), rf"\1{HIDDEN}@"),
    (
        re.compile(
            r"(?im)\b(authorization|proxy-authorization|set-cookie|cookie)([\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\n]*)"
        ),
        rf"\1\2{HIDDEN}",
    ),
    (re.compile(r"(?i)\b(basic|bearer|token)(\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1\2{HIDDEN}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"), HIDDEN),
    (re.compile(r"\b(?:sk-ant-|sk-proj-|sk-|sb_secret_|sb_publishable_|ghs_|ghp_|github_pat_|rnd_)[A-Za-z0-9_-]{8,}"), HIDDEN),
    (re.compile(r"(?i)([?&](?:token|key|secret|password|code|sig)=)[^&\s#]+"), rf"\1{HIDDEN}"),
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api_key|apikey|access_code|cookie)[\w.-]*)"
            r"([\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;}]+)"
        ),
        rf"\1\2{HIDDEN}",
    ),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), HIDDEN),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), HIDDEN),
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b"), HIDDEN),
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?"), HIDDEN),
)
SECRET_WORD = re.compile(r"(?i)key|token|secret|password")
LONG_VALUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def secrets_of(settings: Settings, extra: Iterable[str | None] = ()) -> list[str]:
    values = [settings.anthropic_api_key, settings.openai_api_key, settings.render_api_key, *extra]
    for url in (settings.database_url, settings.codes_database_url):
        values.append(url)
        try:
            values.append(urlsplit(url).password)
        except ValueError:
            pass
    return sorted({value for value in values if value and len(value) >= 8}, key=len, reverse=True)


def sanitize(text: str, secrets: Iterable[str] = ()) -> str:
    for value in secrets:
        text = text.replace(value, HIDDEN)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return "\n".join(
        LONG_VALUE.sub(HIDDEN, line) if SECRET_WORD.search(line) else line for line in text.split("\n")
    )


def clip(text: str, limit: int = SOURCE_LIMIT, tail: bool = False) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = f"[обрезано: {len(data)} байтов]\n" if tail else f"\n[обрезано: {len(data)} байтов]"
    room = limit - len(marker.encode("utf-8"))
    if tail:
        return marker + data[-room:].decode("utf-8", "ignore")
    return data[:room].decode("utf-8", "ignore") + marker


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


class ReworkMaterials:
    def __init__(self, settings: Settings, store: TaskStore, workspace: Any) -> None:
        self.settings = settings
        self.store = store
        self.workspace = workspace
        self.github: GitHubClient | None = None
        self.render: RenderClient | None = None
        self._owner: str | None = None

    def secrets(self) -> list[str]:
        token = getattr(self.github.auth, "_token", None) if self.github else None
        return secrets_of(self.settings, [token])

    def manual_rollback(self, release: ReleaseCandidate | None) -> ReleaseCandidate | None:
        if release is None or release.status != ReleaseStatus.rolled_back:
            return None
        with self.store.db.connection() as conn:
            row = conn.fetchone(
                "SELECT * FROM release_candidates WHERE kind = :kind AND expected_previous_sha = :sha "
                "AND status = :status ORDER BY created_at DESC, id DESC LIMIT 1",
                {"kind": ReleaseKind.rollback.value, "sha": release.commit_sha, "status": ReleaseStatus.released.value},
            )
        return candidate_from_row(row) if row else None

    def collect(self, task: Task, context: dict[str, Any]) -> list[dict[str, str]]:
        release = self.store.get_candidate(context["release_id"]) if context.get("release_id") else None
        rollback = self.manual_rollback(release)
        items = [
            ("Причина отката", lambda: self.reason(task, context, release, rollback), False),
            ("События деплоя", lambda: self.deploys(task, [item for item in (release, rollback) if item]), False),
            ("Логи Render", lambda: self.render_logs(release, rollback), True),
            ("Лог упавшей проверки GitHub Actions", lambda: self.actions_log(task, release), True),
            ("Исходный diff задачи", lambda: self.source_diff(context), False),
        ]
        raw = []
        for title, read, tail in items:
            try:
                body = read()
            except Exception as error:
                body = f"{UNAVAILABLE}: {error or type(error).__name__}"
            raw.append((title, body, tail))
        secrets = self.secrets()
        return [{"title": title, "body": clip(sanitize(body, secrets), tail=tail)} for title, body, tail in raw]

    def reason(
        self, task: Task, context: dict[str, Any], release: ReleaseCandidate | None, rollback: ReleaseCandidate | None
    ) -> str:
        source = context.get("source", "")
        lines = [
            f"Источник: {source} — {SOURCES.get(source, 'нет данных')}.",
            f"Причина: {task.rework_reason or 'нет данных'}",
            f"Комментарий администратора: {context.get('comment') or 'нет'}",
        ]
        if release is not None:
            if release.verify is not None:
                lines.append(f"Проверка после деплоя: {release.verify.model_dump_json()}")
            if release.rollback is not None:
                lines.append(f"Откат: {release.rollback.model_dump_json()}")
            if release.deploy is not None and release.deploy.error:
                lines.append(f"Ошибка деплоя: {release.deploy.error}")
            if release.e2e is not None:
                failed = [f"{check.command} (exit {check.exit_code})" for check in release.e2e.checks if check.exit_code]
                if failed:
                    lines.append(release.blocked_reason or f"Критический E2E итогового commit: {', '.join(failed)}")
        if rollback is not None:
            lines.append(f"Ручной откат {rollback.id}: причина — {rollback.reason or 'не указана'}")
            if rollback.verify is not None:
                lines.append(f"Проверка после ручного отката: {rollback.verify.model_dump_json()}")
        return "\n".join(lines)

    def deploys(self, task: Task, related: list[ReleaseCandidate]) -> str:
        if not related:
            return f"{UNAVAILABLE}: выпуск не найден"
        names = {item.id for item in related}
        events = [
            f"{event.timestamp.isoformat()} {event.type.value}: {event.payload}"
            for event in self.store.events_after(task.id, 0, limit=100000)
            if event.type in (TaskEventType.deploy_status, TaskEventType.rollback_status)
            and event.payload.get("release_id") in names
        ]
        ids = [
            deploy_id
            for item in related
            for deploy_id in (
                item.deploy.render_deploy_id if item.deploy else None,
                item.rollback.render_deploy_id if item.rollback else None,
            )
            if deploy_id
        ]
        if not ids:
            return "\n".join(events + ["Деплоя не было: проверки итогового commit не пройдены до выпуска."])
        if self.render is None:
            return "\n".join(events + [f"{UNAVAILABLE}: Render не настроен"])
        for deploy_id in ids:
            deploy = self.render.get_deploy(deploy_id)
            fields = {name: deploy.get(name) for name in ("id", "status", "trigger", "createdAt", "finishedAt")}
            events.append(f"Render {deploy_id}: {fields}")
        return "\n".join(events)

    def window_end(self, release: ReleaseCandidate, rollback: ReleaseCandidate | None) -> datetime | None:
        ends: list[datetime | None] = []
        if rollback is not None:
            ends += [rollback.verify.checked_at if rollback.verify else None]
            ends += [rollback.deploy.finished_at if rollback.deploy else None]
        if release.rollback is not None:
            ends += [release.rollback.verify.checked_at if release.rollback.verify else None, release.rollback.finished_at]
        ends += [release.verify.checked_at if release.verify else None, release.deploy.finished_at]
        return next((moment for moment in ends if moment is not None), None)

    def render_logs(self, release: ReleaseCandidate | None, rollback: ReleaseCandidate | None) -> str:
        if release is None or release.deploy is None:
            return "Деплоя не было: логов Render нет."
        if self.render is None:
            return f"{UNAVAILABLE}: Render не настроен"
        if self._owner is None:
            self._owner = str(self.render.service().get("ownerId") or "")
        if not self._owner:
            return f"{UNAVAILABLE}: ownerId сервиса Render не получен"
        build = release.deploy.render_status == "build_failed"
        start, end = _iso(release.deploy.started_at), _iso(self.window_end(release, rollback))
        lines = []
        for _ in range(LOG_PAGES):
            data = self.render.logs(self._owner, start, end)
            for item in data.get("logs") or []:
                labels = {label.get("name"): label.get("value") for label in item.get("labels") or []}
                message = str(item.get("message", ""))
                kind, level = labels.get("type"), str(labels.get("level", "")).lower()
                if kind == "build" and not build:
                    continue
                if kind != "build" and (ACCESS_LINE.search(message) or not (
                    level in LOG_LEVELS or message.startswith(("Traceback", "  File "))
                )):
                    continue
                lines.append(f"{item.get('timestamp', '')} {kind or ''} {level} {message}")
            if not data.get("hasMore") or not data.get("nextStartTime"):
                break
            start, end = str(data["nextStartTime"]), str(data.get("nextEndTime") or end)
        return "\n".join(lines) or "Строк уровня warning/error и трассировок за окно деплоя нет."

    def actions_log(self, task: Task, release: ReleaseCandidate | None) -> str:
        if release is None or not release.ci_run_ids or release.deploy is not None:
            return "Не применимо: упавших проверок GitHub Actions итогового commit нет."
        if self.github is None:
            return f"{UNAVAILABLE}: GitHub App не настроен"
        parts = []
        for run_id in release.ci_run_ids:
            for job in self.github.jobs(int(run_id)):
                if job.get("conclusion") == "failure":
                    parts.append(f"Job {job.get('name')}:\n{self.github.job_log(int(job['id']))}")
        return "\n\n".join(parts) or "Упавших job не найдено."

    def source_diff(self, context: dict[str, Any]) -> str:
        sha = context.get("merge_sha")
        if not sha:
            return f"{UNAVAILABLE}: исходное слияние не записано"
        return self.workspace.source_diff(sha, DIFF_READ)
