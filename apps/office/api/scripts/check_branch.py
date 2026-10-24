"""Проверка ветки задачи офиса по политике путей: каждый изменённый путь, режим файла и добавленные строки.

Запуск из каталога apps/office/api: uv run python -m scripts.check_branch --repo <корень монорепозитория> --base <sha> --head <sha>
Diff считается от merge-base базового и проверяемого commit. Политика читается из checkout офиса, в котором лежит скрипт;
из проверяемой ревизии читаются только объекты git, код из неё не запускается.
Код возврата: 0 — нарушений нет; 1 — есть нарушения или проверка не выполнена.
"""

import argparse
import re
import sys
from pathlib import Path

from app.branch_check import WorkspaceError, check_range, run_git
from app.config import POLICIES_DIR
from app.paths import PathPolicy

SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    if not all(SHA.fullmatch(value) for value in (args.base, args.head)):
        print("--base и --head должны быть полными SHA commit.", file=sys.stderr)
        return 1
    policy = PathPolicy(POLICIES_DIR / "paths.yaml")
    try:
        head = run_git("rev-parse", "--verify", f"{args.head}^{{commit}}", cwd=args.repo).decode().strip()
        base = run_git("merge-base", args.base, head, cwd=args.repo).decode().strip()
        found = check_range(policy, args.repo, base, head)
    except WorkspaceError as error:
        print(f"Проверка не выполнена: {error}", file=sys.stderr)
        return 1
    if found:
        print(f"Ветка {head} нарушает политику путей офиса:")
        for item in found:
            print(f"- {item}")
        return 1
    print(f"Ветка {head}: нарушений нет (база {base}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
