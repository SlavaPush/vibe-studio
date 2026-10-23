import mimetypes
import re
from pathlib import Path

from fastapi.responses import FileResponse

from app.errors import ErrorCode, OfficeError, not_found

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TEXT_TYPES = {".log": "text/plain", ".txt": "text/plain", ".md": "text/markdown"}
ARCHIVES = {".zip", ".gz", ".tgz", ".tar"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def check_name(name: str) -> None:
    if not NAME.fullmatch(name) or ".." in name:
        raise OfficeError(400, ErrorCode.invalid_artifact_name, "Недопустимое имя артефакта.")


def content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_TYPES:
        return f"{TEXT_TYPES[suffix]}; charset=utf-8"
    guessed, _ = mimetypes.guess_type(name)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed in {"image/svg+xml", "application/json"}:
        return f"{guessed}; charset=utf-8"
    return guessed


def artifact_response(artifacts_dir: Path, task_id: str, name: str) -> FileResponse:
    check_name(name)
    missing = not_found("Артефакт не найден.")
    root = artifacts_dir.resolve()
    task_path = artifacts_dir / task_id
    candidate = task_path / name
    if task_path.is_symlink() or candidate.is_symlink() or not candidate.is_file():
        raise missing
    task_dir = task_path.resolve()
    resolved = candidate.resolve()
    if task_dir.parent != root or task_dir.name != task_id or resolved.parent != task_dir:
        raise missing
    headers = dict(SECURITY_HEADERS)
    if Path(name).suffix.lower() in ARCHIVES:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(resolved, media_type=content_type(name), headers=headers)
