import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal
from xml.etree import ElementTree

import yaml

from app.artifacts import check_name
from app.config import APP_DIR, SANDBOX_DIR, Settings
from app.errors import OfficeError
from app.schemas import AgentRole, ChangeClass, CheckReport, CheckResult, TaskEventType
from app.tasks import TaskStore
from app.workspace import QA_API_DIR, QA_DIR, Workspace

logger = logging.getLogger("office.sandbox")

INSTALL = "install"
LOCK = "lock"
SUITE = ("lint", "test", "build", "e2e")
API_TEST = "api-test"
API_COMPAT = "api-compat"
API_PREFIXES = (f"{APP_DIR}/api/", f"{APP_DIR}/contracts/")
SCHEMA_PATH = f"{APP_DIR}/api/app/schema.sql"
JUNIT_SUFFIX = "junit.xml"
API_TEST_COMMAND = "python -m pytest -p no:cacheprovider"
COMPAT_COMMANDS = (
    "api-compat: pytest базовой версии",
    "api-compat: схема версии задачи поверх данных базовой версии",
    "api-compat: pytest базовой версии на новой схеме",
)
PYTHON_STEP = SANDBOX_DIR.parent / "sandbox-python" / "step.sh"
TIMEOUT_EXIT = 124
BUILD_EXIT = 90
UNCACHED_EXITS = frozenset({TIMEOUT_EXIT, 125, 126, 127, 137})
LABEL = "back-office.workspace"
REPORT_NAME = "playwright-report.json"
UNMOCKED_NAME = "unmocked-api.txt"
UNMOCKED_SHOWN = 5
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ASSERTION = re.compile(r"^(Error: )?(expect(\.soft)?\(|Timed out \d+ms waiting for expect\()")
INFRA = re.compile(
    r"Executable doesn't exist|browserType\.launch|net::ERR_CONNECTION_REFUSED|ECONNREFUSED"
    r"|exceeded (during setup|while setting up)"
)
MESSAGE_LIMIT = 1000
TEST_ERROR_LIMIT = 1500
SANDBOX_USER = "10001:10001"
MARKER = "/deps/.office-complete"
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
BASE_TEST = re.compile(
    rf"(^|/)[^/]+\.(test|spec)\.[^/]+$|^{re.escape(APP_DIR)}/web/src/test/|^{re.escape(APP_DIR)}/api/tests/"
)
UNAVAILABLE = "Sandbox недоступен: образ не найден или Docker не запущен. Соберите образ командой из README."

MAX_CONTAINERS = 2


class CappedLog:
    def __init__(self, file: BinaryIO, limit: int) -> None:
        self.file = file
        self.limit = limit
        self.size = 0
        self.truncated = False

    def write(self, data: bytes) -> None:
        room = self.limit - self.size
        if room > 0:
            self.file.write(data[:room])
            self.size += min(room, len(data))
        if len(data) > room:
            self.truncated = True


Runner = Callable[[list[str], float, CappedLog | None], int]


def run_process(args: list[str], timeout: float, log: CappedLog | None) -> int:
    if log is None:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout
        ).returncode
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def pump() -> None:
        while chunk := proc.stdout.read1(65536):
            log.write(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        reader.join()
        proc.stdout.close()


class SandboxError(Exception):
    pass


class StepDenied(SandboxError):
    pass


class SandboxCancelled(Exception):
    pass


@dataclass(frozen=True)
class E2EResult:
    status: Literal["passed", "failed", "test_error", "invalid", "no_evidence"]
    reason: str = ""
    line: int | None = None
    message: str = ""


def unmocked_requests(task_dir: Path, artifact_urls: list[str]) -> list[str]:
    for url in artifact_urls:
        name = url.rsplit("/", 1)[-1]
        if name.endswith(UNMOCKED_NAME):
            try:
                text = (task_dir / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ["список запросов недоступен"]
            return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
    return []


def unmocked_reason(requests: list[str]) -> str:
    shown = ", ".join(requests[:UNMOCKED_SHOWN])
    if len(requests) > UNMOCKED_SHOWN:
        shown += f" и ещё {len(requests) - UNMOCKED_SHOWN}"
    return (
        f"Тест QA обратился к API vibe-chat без подмены: {shown}. Backend в sandbox недоступен, поэтому результат "
        "теста не проверяет критерии. Подмените все запросы к /api/ через page.route до page.goto ответами по контракту API."
    )


def _specs(suite: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from suite.get("specs") or []
    for child in suite.get("suites") or []:
        yield from _specs(child)


def error_message(error: dict[str, Any]) -> str:
    return ANSI.sub("", str(error.get("message") or "")).strip()


def is_assertion(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    lines = error_message(error).splitlines()
    return bool(lines) and ASSERTION.match(lines[0]) is not None


def is_infra(error: dict[str, Any]) -> bool:
    return INFRA.search(f"{error_message(error)}\n{error.get('stack') or ''}") is not None


def test_line(error: dict[str, Any], test_file: str) -> int | None:
    location = error.get("location")
    if isinstance(location, dict):
        file = str(location.get("file") or "")
        line = location.get("line")
        if (file == test_file or file.endswith(f"/{test_file}")) and isinstance(line, int) and line >= 1:
            return line
    text = f"{error_message(error)}\n{ANSI.sub('', str(error.get('stack') or ''))}"
    found = re.search(rf"(?:^|[\s(/]){re.escape(test_file)}:(\d+)", text, re.MULTILINE)
    return int(found.group(1)) if found and int(found.group(1)) >= 1 else None


def parse_playwright(report: Any, test_file: str) -> E2EResult:
    if not isinstance(report, dict):
        return E2EResult("no_evidence", "Нет отчёта Playwright.")
    if report.get("errors"):
        return E2EResult("no_evidence", "Playwright завершился с глобальной ошибкой: сервер или браузер не запустились.")
    tests = [
        test
        for suite in report.get("suites") or []
        for spec in _specs(suite)
        if str(spec.get("file", "")).endswith(test_file)
        for test in spec.get("tests") or []
    ]
    if not tests:
        return E2EResult("no_evidence", "Отчёт не содержит тестов QA.")
    failure: dict[str, Any] | None = None
    broken: tuple[dict[str, Any], int] | None = None
    for test in tests:
        results = test.get("results") or []
        status = results[-1].get("status") if results else None
        if test.get("status") == "skipped" or status not in ("passed", "failed", "timedOut"):
            return E2EResult("no_evidence", "Тест QA пропущен или прерван.")
        if status == "passed":
            continue
        errors = [error for error in results[-1].get("errors") or [] if isinstance(error, dict)]
        if any(is_infra(error) for error in errors):
            return E2EResult("no_evidence", "Тест QA не выполнен: браузер, сервер приложения или fixture не запустились.")
        assertions = [error for error in errors if is_assertion(error)]
        found = [
            (error, line)
            for error in errors
            if not is_assertion(error) and (line := test_line(error, test_file)) is not None
        ]
        if assertions and not found:
            failure = failure or assertions[0]
            continue
        if not found:
            return E2EResult("no_evidence", "Тест QA завершился ошибкой без проверки утверждения вне файла теста.")
        broken = broken or found[0]
    if broken is not None:
        error, line = broken
        return E2EResult(
            "test_error",
            "Тест QA завершился ошибкой до проверки утверждения.",
            line=line,
            message=error_message(error)[:TEST_ERROR_LIMIT],
        )
    if failure is None:
        return E2EResult("passed")
    location = failure.get("location") or {}
    line = location.get("line")
    return E2EResult(
        "failed",
        line=line if isinstance(line, int) and line >= 1 else None,
        message=error_message(failure)[:MESSAGE_LIMIT],
    )


def suite_steps(changed: list[str], change_class: ChangeClass | None, playwright: bool) -> list[str]:
    steps = [*SUITE[:3], *(["e2e"] if playwright else [])]
    if change_class == ChangeClass.backend_api or any(path.startswith(API_PREFIXES) for path in changed):
        steps.append(API_TEST)
    if SCHEMA_PATH in changed:
        steps.append(API_COMPAT)
    return steps


def suite_commands(policy: "CommandsPolicy", steps: list[str]) -> list[str]:
    commands = []
    for name in steps:
        if name == API_TEST:
            commands.append(API_TEST_COMMAND)
        elif name == API_COMPAT:
            commands.extend(COMPAT_COMMANDS)
        else:
            commands.append(policy.steps[name].command)
    return commands


def junit_case(case: ElementTree.Element, module: str) -> bool:
    return module in (case.get("classname") or "").split(".") or (case.get("name") or "").endswith(module)


def parse_junit(data: bytes | None, test_file: str) -> E2EResult:
    if data is None:
        return E2EResult("no_evidence", "Нет отчёта pytest.")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return E2EResult("no_evidence", "Отчёт pytest повреждён.")
    name = PurePosixPath(test_file).name
    module = PurePosixPath(test_file).stem
    cases = [case for case in root.iter("testcase") if junit_case(case, module)]
    if not cases:
        return E2EResult("no_evidence", "Отчёт pytest не содержит тестов QA.")
    failure: tuple[str, int | None] | None = None
    for case in cases:
        if case.find("skipped") is not None:
            return E2EResult("no_evidence", "Тест QA пропущен.")
        for kind in ("error", "failure"):
            found = case.find(kind)
            if found is None:
                continue
            text = f"{found.get('message') or ''}\n{found.text or ''}".strip()
            lines = re.findall(rf"{re.escape(name)}:(\d+):", text)
            line = int(lines[-1]) if lines else None
            if kind == "error" or "AssertionError" not in text:
                return E2EResult(
                    "test_error", "Тест QA завершился ошибкой до проверки утверждения.", line=line,
                    message=text[:TEST_ERROR_LIMIT],
                )
            failure = failure or (text[:MESSAGE_LIMIT], line)
    if failure is None:
        return E2EResult("passed")
    return E2EResult("failed", line=failure[1], message=failure[0])


@dataclass(frozen=True)
class Step:
    name: str
    command: str
    network: bool
    timeout: float
    roles: frozenset[AgentRole]


class CommandsPolicy:
    def __init__(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        limits = data["limits"]
        self.memory = str(limits["memory"])
        self.cpus = str(limits["cpus"])
        self.pids = int(limits["pids"])
        self.tmpfs_size = str(limits["tmpfs_size"])
        self.log_bytes = int(limits["log_bytes"])
        self.artifact_bytes = int(limits["artifact_bytes"])
        self.steps = {
            name: Step(
                name=name,
                command=spec["command"],
                network=bool(spec["network"]),
                timeout=float(spec["timeout_seconds"]),
                roles=frozenset(AgentRole(role) for role in spec["roles"]),
            )
            for name, spec in data["steps"].items()
        }


def mount(kind: str, source: str | Path, target: str, *, readonly: bool) -> list[str]:
    if "," in str(source):
        raise SandboxError("Путь для mount содержит запятую.")
    spec = f"type={kind},source={source},target={target}"
    return ["--mount", spec + (",readonly" if readonly else "")]


def regular_files(root: Path) -> list[Path]:
    found = []
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                found.append(path)
    return sorted(found)


def tree_entries(root: Path, names: list[str] | None) -> list[tuple[str, str, Path]] | None:
    found = []
    for directory, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            mode = os.lstat(path).st_mode
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                found.append((rel, "link", path))
            elif stat.S_ISREG(mode):
                found.append((rel, "exec" if mode & 0o111 else "file", path))
    if names is not None and {rel for rel, _, _ in found} - set(names):
        return None
    return sorted(found)


class Sandbox:
    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        workspace: Workspace,
        *,
        runner: Runner = run_process,
        volume_prefix: str = "office-nm-",
    ) -> None:
        if not 1 <= settings.sandbox_containers <= MAX_CONTAINERS:
            raise ValueError(f"OFFICE_SANDBOX_CONTAINERS: ожидается число от 1 до {MAX_CONTAINERS}.")
        self.policy = CommandsPolicy(settings.commands_policy)
        self.policy_files = (
            settings.commands_policy,
            SANDBOX_DIR / "office-e2e" / "playwright.config.ts",
            SANDBOX_DIR / "office-e2e" / "serve.mjs",
            SANDBOX_DIR / "step.sh",
            PYTHON_STEP,
        )
        self.image = settings.sandbox_image
        self.artifacts_dir = settings.artifacts_dir
        self.store = store
        self.workspace = workspace
        self.runner = runner
        self.volume_prefix = volume_prefix
        self.slots = threading.BoundedSemaphore(settings.sandbox_containers)
        self._guard = threading.Lock()
        self._install_locks: dict[str, threading.Lock] = {}
        self._task_locks: dict[str, threading.Lock] = {}
        self._containers: dict[str, set[str]] = {}
        self._cancelled: set[str] = set()
        digest = hashlib.sha256(str(workspace.root).encode("utf-8")).hexdigest()[:16]
        self.label = f"{LABEL}={digest}"
        from app.python_sandbox import PythonSandbox

        self.python = PythonSandbox(self)

    def remove_stale(self) -> None:
        output = io.BytesIO()
        log = CappedLog(output, 1024 * 1024)
        try:
            code = self.runner(["docker", "ps", "-aq", "--filter", f"label={self.label}"], 60, log)
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("docker ps failed")
            return
        if code != 0:
            return
        for container in output.getvalue().decode("utf-8", "replace").split():
            self._docker("rm", "-f", container, timeout=30)

    def base_args(self, name: str, *, network: bool) -> list[str]:
        limits = self.policy
        args = [
            "docker", "create", "--rm", "--pull", "never", "--name", name,
            "--label", self.label,
            "--log-driver", "none",
            "--user", SANDBOX_USER,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={limits.tmpfs_size}",
            "--tmpfs", f"/work:rw,uid=10001,gid=10001,mode=0755,size={limits.tmpfs_size}",
            "--memory", limits.memory,
            "--cpus", limits.cpus,
            "--pids-limit", str(limits.pids),
            "--env", "CI=true",
            "--env", "HOME=/tmp",
        ]
        if not network:
            args += ["--network", "none"]
        return args

    def step_args(
        self, name: str, step: Step, *, src: Path, volume: str, out: Path | None, qa: Path | None
    ) -> list[str]:
        args = self.base_args(name, network=step.network)
        args += mount("bind", src, "/src", readonly=True)
        args += mount("volume", volume, "/deps", readonly=step.name != INSTALL)
        if out is not None:
            args += mount("bind", out, "/out", readonly=False)
        if qa is not None:
            args += mount("bind", qa, "/qa", readonly=True)
        return args + [self.image, "sandbox-step", step.name, step.command]

    def _docker(self, *args: str, timeout: float = 60) -> int:
        try:
            return self.runner(["docker", *args], timeout, None)
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("docker %s failed", args[0])
            return -1

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._guard:
            return self._task_locks.setdefault(task_id, threading.Lock())

    def cancelled(self, task_id: str) -> bool:
        with self._guard:
            return task_id in self._cancelled

    def check_cancelled(self, task_id: str) -> None:
        if self.cancelled(task_id):
            raise SandboxCancelled("Выполнение задачи отменено.")

    def cancel_task(self, task_id: str) -> None:
        with self._guard:
            self._cancelled.add(task_id)
        with self._task_lock(task_id):
            names = sorted(self._containers.get(task_id, ()))
        for name in names:
            self._docker("rm", "-f", name, timeout=30)

    def reset_task(self, task_id: str) -> None:
        with self._guard:
            self._cancelled.discard(task_id)

    def has_containers(self, task_id: str) -> bool:
        with self._task_lock(task_id):
            return bool(self._containers.get(task_id))

    def _container(
        self, task_id: str, name: str, args: list[str], timeout: float, log: CappedLog | None
    ) -> int:
        with self.slots:
            lock = self._task_lock(task_id)
            with lock:
                self.check_cancelled(task_id)
                created = self._docker(*args[1:], timeout=60)
                if created != 0:
                    self._docker("rm", "-f", name, timeout=30)
                    return created
                self._containers.setdefault(task_id, set()).add(name)
                if self.cancelled(task_id):
                    self._docker("rm", "-f", name, timeout=30)
                    self._containers[task_id].discard(name)
                    raise SandboxCancelled("Выполнение задачи отменено.")
            try:
                try:
                    exit_code = self.runner(["docker", "start", "--attach", name], timeout, log)
                except subprocess.TimeoutExpired:
                    self._docker("kill", name, timeout=30)
                    exit_code = TIMEOUT_EXIT
            finally:
                with lock:
                    self._containers[task_id].discard(name)
            self.check_cancelled(task_id)
            return exit_code

    def require_image(self) -> None:
        if self._docker("image", "inspect", self.image) != 0:
            raise SandboxError(UNAVAILABLE)

    def image_id(self, image: str) -> str | None:
        output = io.BytesIO()
        try:
            code = self.runner(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image], 60, CappedLog(output, 4096)
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("docker image inspect failed")
            return None
        found = output.getvalue().decode("utf-8", "replace").strip()
        return found if code == 0 and found else None

    def tree_hash(self, images: list[str], trees: list[tuple[str, Path]]) -> str | None:
        digest = hashlib.sha256()
        for image in images:
            found = self.image_id(image)
            if found is None:
                return None
            digest.update(f"image\0{image}\0{found}\0".encode())
        items = [("policy", path.name, "file", path) for path in self.policy_files]
        for label, root in trees:
            entries = tree_entries(root, self.workspace.commit_files(root))
            if entries is None:
                return None
            items += [(label, rel, kind, path) for rel, kind, path in entries]
        for label, rel, kind, path in items:
            data = os.readlink(path).encode() if kind == "link" else path.read_bytes()
            digest.update(f"{label}\0{rel}\0{kind}\0{len(data)}\0".encode())
            digest.update(data)
        return f"sha256:{digest.hexdigest()}"

    def cached(self, task_id: str, key: str | None, name: str) -> CheckResult | None:
        return None if key is None else self.store.cached_check(task_id, key, name)

    def remember(self, task_id: str, key: str | None, name: str, result: CheckResult) -> None:
        if key is not None and result.exit_code >= 0 and result.exit_code not in UNCACHED_EXITS:
            self.store.save_check(task_id, key, name, result)

    def replay(
        self, task_id: str, result: CheckResult, step: str, role: AgentRole | None, context: dict[str, str] | None
    ) -> CheckResult:
        payload = {
            "command": result.command, "exit_code": result.exit_code, "duration_ms": result.duration_ms,
            "step": step, "cached": True,
        }
        self.store.add_event(
            task_id,
            TaskEventType.check_result,
            payload | (context or {}),
            role=role,
            tool="sandbox",
            artifact=result.artifact_urls[0].rsplit("/", 1)[-1] if result.artifact_urls else None,
        )
        return result

    def volume_ready(self, task_id: str, volume: str) -> bool:
        if self._docker("volume", "inspect", volume) != 0:
            return False
        name = f"office-probe-{os.urandom(4).hex()}"
        args = self.base_args(name, network=False)
        args += mount("volume", volume, "/deps", readonly=True)
        args += [self.image, "test", "-f", MARKER]
        return self._container(task_id, name, args, 60, None) == 0

    def _install_lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._install_locks.setdefault(key, threading.Lock())

    def _next_number(self, task_dir: Path, step: str) -> int:
        with self._guard:
            task_dir.mkdir(parents=True, exist_ok=True)
            number = 1
            while (task_dir / f"{step}-{number}.log").exists():
                number += 1
            (task_dir / f"{step}-{number}.log").touch()
            return number

    def _collect(self, out: Path, task_dir: Path, prefix: str) -> list[str]:
        names = []
        for index, path in enumerate(regular_files(out)):
            if path.stat().st_size > self.policy.artifact_bytes:
                continue
            head = f"{prefix}-{index}-"
            clean = UNSAFE.sub("_", path.name).lstrip("._-") or "file"
            name = (head + clean[-(128 - len(head)):]).replace("..", "_")
            try:
                check_name(name)
            except OfficeError:
                continue
            shutil.copyfile(path, task_dir / name)
            names.append(name)
        return names

    def _run(
        self,
        task_id: str,
        step: Step,
        *,
        src: Path,
        volume: str,
        qa: Path | None = None,
        role: AgentRole | None = None,
        context: dict[str, str] | None = None,
        build: Callable[[str, Path], list[str]] | None = None,
        keep: Callable[[Path], None] | None = None,
        key: str | None = None,
    ) -> CheckResult:
        task_dir = self.artifacts_dir / task_id
        number = self._next_number(task_dir, step.name)
        name = f"office-{task_id}-{step.name}-{number}"
        log_name = f"{step.name}-{number}.log"
        out = Path(tempfile.mkdtemp(dir=self.workspace.root, prefix=".out-"))
        try:
            out.chmod(0o777)
            if build is not None:
                args = build(name, out)
            else:
                args = self.step_args(
                    name, step, src=src, volume=volume, out=None if step.name == INSTALL else out, qa=qa
                )
            started = time.monotonic()
            with open(task_dir / log_name, "wb") as file:
                log = CappedLog(file, self.policy.log_bytes)
                exit_code = self._container(task_id, name, args, step.timeout, log)
                if log.truncated:
                    file.write("\n[журнал обрезан]\n".encode())
            duration_ms = int((time.monotonic() - started) * 1000)
            if keep is not None:
                if exit_code == 0:
                    keep(out)
                names = [log_name]
            else:
                names = [log_name, *self._collect(out, task_dir, f"{step.name}-{number}")]
        finally:
            shutil.rmtree(out, ignore_errors=True)
        result = CheckResult(
            command=step.command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            artifact_urls=[f"/api/tasks/{task_id}/artifacts/{item}" for item in names],
        )
        payload = {"command": step.command, "exit_code": exit_code, "duration_ms": duration_ms, "step": step.name}
        self.store.add_event(
            task_id,
            TaskEventType.check_result,
            payload | (context or {}),
            role=role,
            tool="sandbox",
            artifact=log_name,
        )
        self.remember(task_id, key, step.command, result)
        return result

    def ensure_deps(
        self, task_id: str, src: Path, context: dict[str, str] | None = None
    ) -> tuple[str, CheckResult | None]:
        lock_file = src / "web" / "package-lock.json"
        if lock_file.is_symlink() or not lock_file.is_file():
            raise SandboxError(f"В рабочей копии нет {APP_DIR}/web/package-lock.json.")
        volume = self.volume_prefix + hashlib.sha256(lock_file.read_bytes()).hexdigest()[:16]
        with self._install_lock(volume):
            if self.volume_ready(task_id, volume):
                return volume, None
            self._docker("volume", "rm", "-f", volume)
            result = self._run(task_id, self.policy.steps[INSTALL], src=src, volume=volume, context=context)
            if result.exit_code != 0:
                self._docker("volume", "rm", "-f", volume)
            elif not self.volume_ready(task_id, volume):
                self._docker("volume", "rm", "-f", volume)
                raise SandboxError("Установка зависимостей не подтвердила готовность тома.")
            return volume, result

    def deny(self, task_id: str, step: str, role: AgentRole | None, reason: str) -> StepDenied:
        self.store.add_event(
            task_id,
            TaskEventType.tool_called,
            {"arguments": step[:40], "result": "denied", "reason": reason},
            role=role,
            tool="run_check",
        )
        return StepDenied(reason)

    def lock_node(self, task_id: str, root: Path) -> CheckResult:
        self.check_cancelled(task_id)
        self.require_image()
        web = root / APP_DIR / "web"
        step = self.policy.steps[LOCK]
        with tempfile.TemporaryDirectory(dir=self.workspace.root) as tmp:
            src = Path(tmp) / "src"
            (src / "web").mkdir(parents=True)
            for name in ("package.json", "package-lock.json"):
                if (web / name).is_symlink() or not (web / name).is_file():
                    raise SandboxError(f"В рабочей копии нет обычного файла {APP_DIR}/web/{name}.")
                shutil.copyfile(web / name, src / "web" / name)
            self._open_tree(src)

            def build(name: str, out: Path) -> list[str]:
                args = self.base_args(name, network=True)
                args += mount("bind", src, "/src", readonly=True)
                args += mount("bind", out, "/out", readonly=False)
                return args + [self.image, "sandbox-step", step.name, step.command]

            def keep(out: Path) -> None:
                result = out / "package-lock.json"
                if result.is_symlink() or not result.is_file():
                    raise SandboxError("Шаг lock не вернул package-lock.json.")
                if result.stat().st_size > self.workspace.paths.max_lock_bytes:
                    raise SandboxError("package-lock.json больше допустимого размера lock-файла.")
                shutil.copyfile(result, web / "package-lock.json")

            return self._run(task_id, step, src=src, volume="", build=build, keep=keep)

    def node_key(self, src: Path, qa: Path | None) -> str | None:
        return self.tree_hash([self.image], [("src", src), *([("qa", qa)] if qa is not None else [])])

    def run_step(self, task_id: str, step_name: str, role: AgentRole | None) -> CheckResult:
        if step_name == API_TEST:
            task = self.store.get_task(task_id)
            if role is not None and (role != AgentRole.developer or task.change_class != ChangeClass.backend_api):
                raise self.deny(task_id, step_name, role, "Шаг доступен только разработчику задачи backend_api.")
            self.check_cancelled(task_id)
            return self.python.run_api_tests(task_id, self.workspace.task_worktree(task_id) / APP_DIR)[-1]
        step = self.policy.steps.get(step_name)
        if step is None or step.name in (INSTALL, LOCK):
            raise self.deny(task_id, step_name, role, "Шага нет в политике команд.")
        if role is not None and role not in step.roles:
            raise self.deny(task_id, step_name, role, "Шаг недоступен этой роли.")
        self.check_cancelled(task_id)
        self.require_image()
        root = self.workspace.task_worktree(task_id)
        src = root / APP_DIR
        with tempfile.TemporaryDirectory(dir=self.workspace.root) as tmp:
            qa = None
            if step.name == "e2e":
                qa = Path(tmp) / "qa"
                if not self._copy_qa(root, qa):
                    raise SandboxError("Набор тестов QA пуст.")
            key = self.node_key(src, qa)
            found = self.cached(task_id, key, step.command)
            if found is not None:
                return self.replay(task_id, found, step.name, role, None)
            volume, install = self.ensure_deps(task_id, src)
            if install is not None and install.exit_code != 0:
                return install
            return self._run(task_id, step, src=src, volume=volume, qa=qa, role=role, key=key)

    def _copy_qa(self, src: Path, qa: Path) -> list[str]:
        files = []
        source = src / QA_DIR
        if source.is_dir() and not source.is_symlink():
            for path in regular_files(source):
                rel = path.relative_to(src).as_posix()
                (qa / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, qa / rel)
                files.append(rel)
        self._open_tree(qa)
        return files

    def _open_tree(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for directory, dirs, names in os.walk(root):
            os.chmod(directory, 0o755)
            for name in names:
                os.chmod(Path(directory) / name, 0o644)

    def suite_hash(self, app: Path, qa: Path | None, extra: list[str] | None = None) -> str:
        items: list[tuple[str, str, bytes]] = []
        for rel in self.workspace.tracked_files(app):
            path = app / rel
            if BASE_TEST.search(rel) and stat.S_ISREG(os.lstat(path).st_mode):
                items.append(("base", rel, path.read_bytes()))
        if qa is not None:
            for path in regular_files(qa):
                items.append(("qa", path.relative_to(qa).as_posix(), path.read_bytes()))
        for rel in extra or []:
            items.append(("qa", rel, (app / rel).read_bytes()))
        for path in self.policy_files:
            items.append(("policy", path.name, path.read_bytes()))
        digest = hashlib.sha256()
        for kind, rel, data in sorted(items):
            digest.update(f"{kind}\0{rel}\0{len(data)}\0".encode())
            digest.update(data)
        return f"sha256:{digest.hexdigest()}"

    def run_suite(
        self, task_id: str, app_sha: str, qa_sha: str, *, require_e2e: bool = False, diff_base: str | None = None,
        change_class: ChangeClass | None = None, reuse: bool = True,
    ) -> CheckReport:
        self.check_cancelled(task_id)
        self.require_image()
        task = self.store.get_task(task_id)
        checks: list[CheckResult] = []
        pytest_qa = require_e2e and change_class == ChangeClass.backend_api
        with (
            self.workspace.checkout(app_sha) as (app, app_full),
            tempfile.TemporaryDirectory(dir=self.workspace.root) as tmp,
        ):
            qa_full = self.workspace.resolve(qa_sha)
            qa = Path(tmp) / "qa"
            qa.mkdir()
            qa_files = self.workspace.export_qa(qa_full, qa) if require_e2e else []
            self._open_tree(qa)
            api_qa = self.workspace.export_qa(qa_full, app, QA_API_DIR) if pytest_qa else []
            if (require_e2e and not pytest_qa and not qa_files) or (pytest_qa and not api_qa):
                raise SandboxError("Набор тестов QA пуст.")
            base = self.workspace.resolve(diff_base or task.base_sha or app_full)
            steps = suite_steps(self.workspace.changed_between(base, app_full), change_class, bool(qa_files))
            suite_hash = self.suite_hash(app, qa if qa_files else None, api_qa)
            context = {"commit_sha": app_full, "qa_sha": qa_full}
            src = app / APP_DIR
            keys = {name: self.node_key(src, qa if name == "e2e" else None) for name in SUITE if name in steps}
            found = {
                name: hit for name, key in keys.items()
                if reuse and (hit := self.cached(task_id, key, self.policy.steps[name].command)) is not None
            }
            volume, install = ("", None) if len(found) == len(keys) else self.ensure_deps(task_id, src, context)
            if install is not None:
                checks.append(install)
            if install is None or install.exit_code == 0:
                for name in keys:
                    self.check_cancelled(task_id)
                    if name in found:
                        checks.append(self.replay(task_id, found[name], name, None, context))
                        continue
                    checks.append(
                        self._run(
                            task_id,
                            self.policy.steps[name],
                            src=src,
                            volume=volume,
                            qa=qa if name == "e2e" else None,
                            context=context,
                            key=keys[name],
                        )
                    )
            if API_TEST in steps:
                checks += self.python.run_api_tests(task_id, src, context, junit=pytest_qa, reuse=reuse)
            if API_COMPAT in steps:
                with self.workspace.checkout(base) as (previous, _):
                    checks += self.python.api_compat(task_id, previous / APP_DIR, src, context, reuse=reuse)
        report = CheckReport(
            task_id=task_id,
            commit_sha=app_full,
            spec_hash=task.spec.spec_hash if task.spec else None,
            suite_hash=suite_hash,
            checks=checks,
        )
        self.store.add_check_report(report)
        return report

    def e2e_result(self, task_id: str, report: CheckReport, test_file: str) -> E2EResult:
        if test_file.endswith(".py"):
            return self.pytest_result(task_id, report, test_file)
        command = self.policy.steps["e2e"].command
        checks = [check for check in report.checks if check.command == command]
        if not checks:
            return E2EResult("no_evidence", "Шаг e2e не выполнялся.")
        check = checks[-1]
        if check.exit_code == BUILD_EXIT:
            return E2EResult("no_evidence", "Сборка приложения для e2e не прошла.")
        if check.exit_code == TIMEOUT_EXIT:
            return E2EResult("no_evidence", "Превышено время шага e2e.")
        unmocked = unmocked_requests(self.artifacts_dir / task_id, check.artifact_urls)
        if unmocked:
            return E2EResult("invalid", unmocked_reason(unmocked))
        data = None
        for url in check.artifact_urls:
            name = url.rsplit("/", 1)[-1]
            if name.endswith(REPORT_NAME):
                try:
                    data = json.loads((self.artifacts_dir / task_id / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = None
                break
        return parse_playwright(data, test_file)

    def pytest_result(self, task_id: str, report: CheckReport, test_file: str) -> E2EResult:
        checks = [check for check in report.checks if check.command == API_TEST_COMMAND]
        if not checks:
            return E2EResult("no_evidence", "Шаг api-test не выполнялся.")
        if checks[-1].exit_code == TIMEOUT_EXIT:
            return E2EResult("no_evidence", "Превышено время шага api-test.")
        for url in checks[-1].artifact_urls:
            name = url.rsplit("/", 1)[-1]
            if name.endswith(JUNIT_SUFFIX):
                try:
                    return parse_junit((self.artifacts_dir / task_id / name).read_bytes(), test_file)
                except OSError:
                    break
        return parse_junit(None, test_file)
