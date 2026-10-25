import hashlib
import io
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.sandbox import (
    API_COMPAT,
    API_TEST,
    API_TEST_COMMAND,
    COMPAT_COMMANDS,
    JUNIT_SUFFIX,
    MARKER,
    CappedLog,
    Sandbox,
    SandboxError,
    mount,
)
from app.schemas import CheckResult, TaskEventType

logger = logging.getLogger("office.python_sandbox")

PYTHON_SANDBOX_IMAGE = "office-python-sandbox:p5"
POSTGRES_IMAGE = "postgres:16-alpine"
INSTALL_STEP = "api-install"
TEST_STEP = API_TEST
INSTALL_COMMAND = "uv sync --locked --no-install-project"
TEST_COMMAND = API_TEST_COMMAND
TEST_ARGS = ("sandbox-python-step", "test")
INSTALL_TIMEOUT = 600
TEST_TIMEOUT = 900
PG_WAIT_SECONDS = 60.0
PG_POLL_SECONDS = 1.0
PG_USER = "70:70"
PG_MEMORY = "512m"
PG_CPUS = "1"
PG_PIDS = 256
TEST_DATABASE_URL = "postgresql://vibe:vibe@postgres:5432/vibe_test"
LOCK_FILES = ("pyproject.toml", "uv.lock")
JUNIT_NAME = JUNIT_SUFFIX
COMPAT_STEP = API_COMPAT
COMPAT_BASE, COMPAT_SCHEMA, COMPAT_RETEST = COMPAT_COMMANDS
APPLY_SCHEMA = (
    "import os, sys, psycopg\n"
    "with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as conn:\n"
    "    conn.execute(open(sys.argv[1], encoding='utf-8').read())\n"
)
LOCK_STEP = "lock-python"
LOCK_COMMAND = "uv lock"
LOCK_TIMEOUT = 600


def env_files(root: Path) -> list[str]:
    found = []
    for directory, dirs, names in os.walk(root):
        for name in [*dirs, *names]:
            if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
                found.append((Path(directory) / name).relative_to(root).as_posix())
    return sorted(found)


class PythonSandbox:
    def __init__(
        self,
        sandbox: Sandbox,
        *,
        image: str = PYTHON_SANDBOX_IMAGE,
        postgres_image: str = POSTGRES_IMAGE,
        volume_prefix: str = "office-py-",
    ) -> None:
        self.sandbox = sandbox
        self.image = image
        self.postgres_image = postgres_image
        self.volume_prefix = volume_prefix

    def require_images(self) -> None:
        for image in (self.image, self.postgres_image):
            if self.sandbox._docker("image", "inspect", image) != 0:
                raise SandboxError(
                    f"Образ {image} не найден или Docker не запущен. Соберите образ песочницы Python командой "
                    f"docker build -t {self.image} apps/office/docker/sandbox-python и загрузите PostgreSQL "
                    f"командой docker pull {self.postgres_image}."
                )

    def check_source(self, src: Path) -> None:
        for name in LOCK_FILES:
            path = src / "api" / name
            if path.is_symlink() or not path.is_file():
                raise SandboxError(f"В рабочей копии нет обычного файла api/{name}.")
        found = env_files(src)
        if found:
            raise SandboxError(f"Рабочая копия содержит файлы окружения: {', '.join(found[:5])}.")

    def remove_stale(self) -> None:
        for listing, removal in ((["ps", "-aq"], ["rm", "-f"]), (["network", "ls", "-q"], ["network", "rm"])):
            output = io.BytesIO()
            try:
                code = self.sandbox.runner(
                    ["docker", *listing, "--filter", f"label={self.sandbox.label}"], 60, CappedLog(output, 1024 * 1024)
                )
            except (OSError, subprocess.TimeoutExpired):
                logger.exception("docker %s failed", listing[0])
                continue
            if code != 0:
                continue
            for item in output.getvalue().decode("utf-8", "replace").split():
                self.sandbox._docker(*removal, item, timeout=30)

    def run_api_tests(
        self, task_id: str, src: Path, context: dict[str, str] | None = None, *, junit: bool = False
    ) -> list[CheckResult]:
        return self._run_api_tests(task_id, src, context, junit=junit)

    def _run_api_tests(
        self,
        task_id: str,
        src: Path,
        context: dict[str, str] | None = None,
        command: list[str] | None = None,
        *,
        junit: bool = False,
    ) -> list[CheckResult]:
        self.sandbox.check_cancelled(task_id)
        self.require_images()
        self.check_source(src)
        volume, install = self.ensure_deps(task_id, src, context)
        checks = [] if install is None else [install]
        if install is not None and install.exit_code != 0:
            return checks
        self.sandbox.check_cancelled(task_id)
        checks.append(self._tests(task_id, src, volume, context, command, junit))
        return checks

    def volume_ready(self, task_id: str, volume: str) -> bool:
        if self.sandbox._docker("volume", "inspect", volume) != 0:
            return False
        name = f"office-probe-{os.urandom(4).hex()}"
        args = self.sandbox.base_args(name, network=False)
        args += mount("volume", volume, "/deps", readonly=True)
        args += [self.image, "test", "-f", MARKER]
        try:
            return self.sandbox._container(task_id, name, args, 60, None) == 0
        finally:
            self.sandbox._docker("rm", "-f", name, timeout=30)

    def ensure_deps(
        self, task_id: str, src: Path, context: dict[str, str] | None
    ) -> tuple[str, CheckResult | None]:
        digest = hashlib.sha256()
        for name in LOCK_FILES:
            data = (src / "api" / name).read_bytes()
            digest.update(f"{name}\0{len(data)}\0".encode())
            digest.update(data)
        volume = self.volume_prefix + digest.hexdigest()[:16]
        with self.sandbox._install_lock(volume):
            if self.volume_ready(task_id, volume):
                return volume, None
            self.sandbox._docker("volume", "rm", "-f", volume)
            complete = False
            files = Path(tempfile.mkdtemp(dir=self.sandbox.workspace.root, prefix=".pydeps-"))
            try:
                files.chmod(0o755)
                for name in LOCK_FILES:
                    shutil.copyfile(src / "api" / name, files / name)
                    (files / name).chmod(0o644)

                def install_args(name: str, out: Path) -> list[str]:
                    args = self.sandbox.base_args(name, network=True)
                    args += mount("bind", files, "/src", readonly=True)
                    args += mount("volume", volume, "/deps", readonly=False)
                    return args + [self.image, "sandbox-python-step", "install"]

                result = self._run(task_id, INSTALL_STEP, INSTALL_COMMAND, install_args, INSTALL_TIMEOUT, context)
                if result.exit_code == 0:
                    if not self.volume_ready(task_id, volume):
                        raise SandboxError("Установка зависимостей API не подтвердила готовность тома.")
                    complete = True
            finally:
                shutil.rmtree(files, ignore_errors=True)
                if not complete:
                    self.sandbox._docker("volume", "rm", "-f", volume)
            return volume, result

    def postgres_args(self, name: str, network: str) -> list[str]:
        size = self.sandbox.policy.tmpfs_size
        return [
            "create", "--pull", "never", "--name", name,
            "--label", self.sandbox.label,
            "--network", network,
            "--network-alias", "postgres",
            "--log-driver", "none",
            "--user", PG_USER,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/var/lib/postgresql/data:rw,uid=70,gid=70,mode=0700,size={size}",
            "--tmpfs", "/var/run/postgresql:rw,uid=70,gid=70,mode=0755,size=16m",
            "--tmpfs", f"/tmp:rw,size={size}",
            "--memory", PG_MEMORY,
            "--cpus", PG_CPUS,
            "--pids-limit", str(PG_PIDS),
            "--env", "POSTGRES_USER=vibe",
            "--env", "POSTGRES_PASSWORD=vibe",
            "--env", "POSTGRES_DB=vibe",
            self.postgres_image,
        ]

    def wait_postgres(self, task_id: str, name: str) -> None:
        deadline = time.monotonic() + PG_WAIT_SECONDS
        while True:
            self.sandbox.check_cancelled(task_id)
            if self.sandbox._docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "vibe", "-d", "vibe") == 0:
                return
            if time.monotonic() >= deadline:
                raise SandboxError(f"PostgreSQL для тестов API не запустился за {int(PG_WAIT_SECONDS)} с.")
            time.sleep(PG_POLL_SECONDS)

    @contextmanager
    def _database(self, task_id: str) -> Iterator[str]:
        suffix = os.urandom(4).hex()
        network = f"office-pynet-{task_id}-{suffix}"
        postgres = f"office-{task_id}-pg-{suffix}"
        try:
            if self.sandbox._docker("network", "create", "--internal", "--label", self.sandbox.label, network) != 0:
                raise SandboxError("Не удалось создать внутреннюю сеть Docker для тестов API.")
            if self.sandbox._docker(*self.postgres_args(postgres, network)) != 0:
                raise SandboxError("Не удалось создать контейнер PostgreSQL для тестов API.")
            if self.sandbox._docker("start", postgres) != 0:
                raise SandboxError("Не удалось запустить контейнер PostgreSQL для тестов API.")
            self.wait_postgres(task_id, postgres)
            yield network
        finally:
            if self.sandbox._docker("rm", "-f", postgres, timeout=30) != 0:
                logger.warning("docker rm %s failed", postgres)
            if self.sandbox._docker("network", "rm", network, timeout=30) != 0:
                logger.warning("docker network rm %s failed", network)

    def _client_args(
        self, name: str, network: str, src: Path, volume: str, command: list[str], out: Path | None = None
    ) -> list[str]:
        args = self.sandbox.base_args(name, network=True)
        args += ["--network", network]
        args += ["--env", f"TEST_DATABASE_URL={TEST_DATABASE_URL}", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        args += mount("bind", src, "/src", readonly=True)
        args += mount("volume", volume, "/deps", readonly=True)
        if out is not None:
            args += mount("bind", out, "/out", readonly=False)
        return args + [self.image, *command]

    def _tests(
        self,
        task_id: str,
        src: Path,
        volume: str,
        context: dict[str, str] | None,
        command: list[str] | None,
        junit: bool = False,
    ) -> CheckResult:
        with self._database(task_id) as network:
            shown = TEST_COMMAND if command is None else shlex.join(command)
            argv = [*(command or TEST_ARGS), *([f"--junitxml=/out/{JUNIT_NAME}"] if junit else [])]
            return self._run(
                task_id, TEST_STEP, shown,
                lambda name, out: self._client_args(name, network, src, volume, argv, out if junit else None),
                TEST_TIMEOUT, context,
            )

    def api_compat(
        self, task_id: str, base_src: Path, task_src: Path, context: dict[str, str] | None = None
    ) -> list[CheckResult]:
        self.sandbox.check_cancelled(task_id)
        self.require_images()
        self.check_source(base_src)
        self.check_source(task_src)
        volume, install = self.ensure_deps(task_id, base_src, context)
        checks = [] if install is None else [install]
        if install is not None and install.exit_code != 0:
            return checks
        steps = (
            (COMPAT_BASE, base_src, list(TEST_ARGS)),
            (COMPAT_SCHEMA, task_src, ["/deps/venv/bin/python", "-c", APPLY_SCHEMA, "/src/api/app/schema.sql"]),
            (COMPAT_RETEST, base_src, list(TEST_ARGS)),
        )
        with self._database(task_id) as network:
            for shown, src, argv in steps:
                self.sandbox.check_cancelled(task_id)
                result = self._run(
                    task_id, COMPAT_STEP, shown,
                    lambda name, out, src=src, argv=argv: self._client_args(name, network, src, volume, argv),
                    TEST_TIMEOUT, context,
                )
                checks.append(result)
                if result.exit_code != 0:
                    break
        return checks

    def lock(self, task_id: str, root: Path) -> CheckResult:
        self.sandbox.check_cancelled(task_id)
        self.require_images()
        api = root / "api"
        files = Path(tempfile.mkdtemp(dir=self.sandbox.workspace.root, prefix=".pylock-"))
        try:
            files.chmod(0o755)
            for name in LOCK_FILES:
                shutil.copyfile(api / name, files / name)
                (files / name).chmod(0o644)

            def lock_args(name: str, out: Path) -> list[str]:
                args = self.sandbox.base_args(name, network=True)
                args += mount("bind", files, "/src", readonly=True)
                args += mount("bind", out, "/out", readonly=False)
                return args + [self.image, "sandbox-python-step", "lock"]

            def keep(out: Path) -> None:
                result = out / "uv.lock"
                if result.is_symlink() or not result.is_file():
                    raise SandboxError("Шаг lock не вернул uv.lock.")
                if result.stat().st_size > self.sandbox.workspace.paths.max_lock_bytes:
                    raise SandboxError("uv.lock больше допустимого размера lock-файла.")
                shutil.copyfile(result, api / "uv.lock")

            return self._run(task_id, LOCK_STEP, LOCK_COMMAND, lock_args, LOCK_TIMEOUT, None, keep)
        finally:
            shutil.rmtree(files, ignore_errors=True)

    def _run(
        self,
        task_id: str,
        step: str,
        command: str,
        build_args: Callable[[str, Path], list[str]],
        timeout: float,
        context: dict[str, str] | None,
        keep: Callable[[Path], None] | None = None,
    ) -> CheckResult:
        task_dir = self.sandbox.artifacts_dir / task_id
        number = self.sandbox._next_number(task_dir, step)
        name = f"office-{task_id}-{step}-{number}"
        log_name = f"{step}-{number}.log"
        names = [log_name]
        out = Path(tempfile.mkdtemp(dir=self.sandbox.workspace.root, prefix=".pyout-"))
        try:
            out.chmod(0o777)
            args = build_args(name, out)
            started = time.monotonic()
            try:
                with open(task_dir / log_name, "wb") as file:
                    log = CappedLog(file, self.sandbox.policy.log_bytes)
                    exit_code = self.sandbox._container(task_id, name, args, timeout, log)
                    if log.truncated:
                        file.write("\n[журнал обрезан]\n".encode())
            finally:
                self.sandbox._docker("rm", "-f", name, timeout=30)
            duration_ms = int((time.monotonic() - started) * 1000)
            junit = out / JUNIT_NAME
            if junit.is_file() and not junit.is_symlink() and junit.stat().st_size <= self.sandbox.policy.artifact_bytes:
                names.append(f"{step}-{number}-{JUNIT_NAME}")
                shutil.copyfile(junit, task_dir / names[-1])
            if exit_code == 0 and keep is not None:
                keep(out)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        result = CheckResult(
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            artifact_urls=[f"/api/tasks/{task_id}/artifacts/{item}" for item in names],
        )
        payload = {"command": command, "exit_code": exit_code, "duration_ms": duration_ms, "step": step}
        self.sandbox.store.add_event(
            task_id,
            TaskEventType.check_result,
            payload | (context or {}),
            tool="sandbox",
            artifact=log_name,
        )
        return result
