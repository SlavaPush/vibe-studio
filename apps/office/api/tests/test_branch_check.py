import json
import uuid

import psycopg
import pytest
from conftest import CHAT_SCHEMA, PG_URL

from app.branch_check import npm_violation, python_violation, schema_violation

BASE = (
    b"CREATE TABLE IF NOT EXISTS users (\n    id uuid PRIMARY KEY,\n    source_hash text NOT NULL\n);\n\n"
    b"CREATE INDEX IF NOT EXISTS users_hash ON users (source_hash);\n"
)


@pytest.mark.parametrize(
    "tail",
    [
        "",
        "\n-- комментарий\n\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname text;\n",
        "alter table users add column if not exists score integer default 0;\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS seen_at timestamptz DEFAULT now();\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS note text DEFAULT 'a;b';\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS flag boolean DEFAULT false;\n",
        "CREATE INDEX IF NOT EXISTS users_created ON users (id, source_hash);\n",
        "CREATE TABLE IF NOT EXISTS notes (\n  id uuid PRIMARY KEY,\n  user_id uuid NOT NULL REFERENCES users (id) "
        "ON DELETE CASCADE,\n  body text NOT NULL CHECK (length(body) > 0),\n  UNIQUE (user_id, body)\n);\n"
        "CREATE INDEX IF NOT EXISTS notes_user ON notes (user_id);\n",
    ],
)
def test_additive_schema_passes(tail):
    assert schema_violation(BASE, BASE + tail.encode()) is False


@pytest.mark.parametrize(
    "tail",
    [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS age integer CHECK (age > 0);\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS age integer NOT NULL DEFAULT 0;\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref uuid REFERENCES users (id);\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS code text UNIQUE;\n",
        "ALTER TABLE users ADD COLUMN age integer;\n",
        "CREATE UNIQUE INDEX IF NOT EXISTS users_unique ON users (source_hash);\n",
        "CREATE INDEX IF NOT EXISTS users_part ON users (id) WHERE source_hash <> '';\n",
        "CREATE INDEX IF NOT EXISTS users_lower ON users (lower(source_hash));\n",
        "CREATE TABLE IF NOT EXISTS notes (id uuid PRIMARY KEY, user_id uuid REFERENCES users (id));\n",
        "CREATE TABLE IF NOT EXISTS notes (id uuid PRIMARY KEY, user_id uuid REFERENCES users (id) ON DELETE SET NULL);\n",
        "CREATE TABLE IF NOT EXISTS notes (id uuid PRIMARY KEY, user_id uuid REFERENCES users ON DELETE RESTRICT);\n",
        "CREATE TABLE IF NOT EXISTS notes (id uuid PRIMARY KEY, u uuid REFERENCES users (id) ON DELETE NO ACTION);\n",
        "CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY);\n",
        "CREATE TABLE IF NOT EXISTS notes (id uuid) INHERITS (users);\n",
        "DROP TABLE users;\n",
        "DROP INDEX users_hash;\n",
        "ALTER TABLE users RENAME COLUMN source_hash TO hash;\n",
        "ALTER TABLE users ALTER COLUMN source_hash DROP NOT NULL;\n",
        "UPDATE users SET source_hash = '';\n",
        "INSERT INTO users (id, source_hash) VALUES (gen_random_uuid(), 'x');\n",
        "DO $$ BEGIN DELETE FROM users; END $$;\n",
        "/* comment */ DELETE FROM users;\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS x text; DELETE FROM users;\n",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS x text DEFAULT 'unterminated;\n",
    ],
)
def test_non_additive_schema_is_rejected(tail):
    assert schema_violation(BASE, BASE + tail.encode()) is True


def test_changed_or_removed_lines_are_rejected():
    middle = BASE.replace(b"\n\nCREATE INDEX", b"\n\nALTER TABLE users ADD COLUMN IF NOT EXISTS x text;\nCREATE INDEX")
    assert schema_violation(BASE, middle) is True
    assert schema_violation(BASE, BASE.replace(b"    source_hash text NOT NULL\n", b"")) is True
    assert schema_violation(BASE, BASE.replace(b"NOT NULL", b"")) is True
    assert schema_violation(BASE, None) is True
    assert schema_violation(None, BASE) is False


def test_rolled_back_version_keeps_data_of_additive_schema():
    if not PG_URL:
        pytest.skip("OFFICE_TEST_POSTGRES_URL не задан")
    base = CHAT_SCHEMA.read_text(encoding="utf-8")
    new = base + (
        "\nALTER TABLE users ADD COLUMN IF NOT EXISTS nickname text;\n"
        "CREATE TABLE IF NOT EXISTS notes (\n    id uuid PRIMARY KEY,\n"
        "    user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,\n    body text NOT NULL\n);\n"
    )
    assert schema_violation(base.encode(), new.encode()) is False
    schema = f"r_{uuid.uuid4().hex[:12]}"
    old_user, new_user, late_user, note = (uuid.uuid4() for _ in range(4))
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path = {schema}")
            conn.execute(base)
            conn.execute("INSERT INTO users (id, source_hash) VALUES (%s, 'old')", (old_user,))
            conn.execute(new)
            conn.execute("INSERT INTO users (id, source_hash, nickname) VALUES (%s, 'new', 'Мария')", (new_user,))
            conn.execute("INSERT INTO notes (id, user_id, body) VALUES (%s, %s, 'заметка')", (note, new_user))
            conn.execute(base)
            conn.execute("INSERT INTO users (id, source_hash) VALUES (%s, 'late')", (late_user,))
            users = conn.execute("SELECT id, source_hash, nickname FROM users").fetchall()
            notes = conn.execute("SELECT id, user_id, body FROM notes").fetchall()
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
    assert sorted(users, key=lambda row: row[1]) == [
        (late_user, "late", None), (new_user, "new", "Мария"), (old_user, "old", None)
    ]
    assert notes == [(note, new_user, "заметка")]


PACKAGE = {
    "name": "web",
    "private": True,
    "scripts": {"test": "vitest run", "lint": "oxlint"},
    "dependencies": {"react": "^19.3.1"},
    "devDependencies": {"vitest": "^5.0.5"},
}


def package(**changes):
    data = json.loads(json.dumps(PACKAGE))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "new",
    [
        package(dependencies={"react": "^19.3.1", "left-pad": "^1.3.0"}),
        package(devDependencies={"vitest": "~5.1.0", "@types/x": ">=1 <2"}),
        package(dependencies={"react": "latest"}),
    ],
)
def test_dependency_changes_in_package_json_pass(new):
    assert npm_violation(json.dumps(PACKAGE).encode(), new) is False


@pytest.mark.parametrize(
    "new",
    [
        package(scripts={"test": "true", "lint": "oxlint"}),
        package(overrides={"react": "18.0.0"}),
        package(engines={"node": ">=24"}),
        package(type="commonjs"),
        package(dependencies={"react": "file:../react"}),
        package(dependencies={"react": "link:../react"}),
        package(dependencies={"react": "git+https://github.com/facebook/react.git"}),
        package(dependencies={"react": "github:facebook/react"}),
        package(dependencies={"react": "https://example.com/react.tgz"}),
        package(dependencies={"react": "facebook/react"}),
        package(dependencies={"react": "npm:preact@10"}),
        package(dependencies={"react": "../react"}),
        package(dependencies=["react"]),
        b"not json",
        None,
    ],
)
def test_other_package_json_changes_are_violations(new):
    assert npm_violation(json.dumps(PACKAGE).encode(), new) is True


PYPROJECT = (
    b'[project]\nname = "vibe-chat-api"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    b'dependencies = [\n    "fastapi>=0.161.1",\n]\n\n[dependency-groups]\ndev = [\n    "pytest>=9.1.1",\n]\n\n'
    b'[tool.pytest.ini_options]\npythonpath = ["."]\ntestpaths = ["tests"]\n'
)


@pytest.mark.parametrize(
    "new",
    [
        PYPROJECT.replace(b'"fastapi>=0.161.1",', b'"fastapi>=0.161.1",\n    "markdown-it-py>=3.0",'),
        PYPROJECT.replace(b'"pytest>=9.1.1",', b'"pytest>=9.1.1",\n    "respx>=0.22",'),
        PYPROJECT.replace(b'"fastapi>=0.161.1"', b'"fastapi[standard]>=0.162; python_version >= \'3.12\'"'),
    ],
)
def test_dependency_changes_in_pyproject_pass(new):
    assert python_violation(PYPROJECT, new) is False


@pytest.mark.parametrize(
    "new",
    [
        PYPROJECT.replace(b'testpaths = ["tests"]', b'testpaths = ["tests/office"]'),
        PYPROJECT + b'\n[tool.pytest.ini_options.markers]\nslow = "x"\n',
        PYPROJECT.replace(b'requires-python = ">=3.12"', b'requires-python = ">=3.11"'),
        PYPROJECT + b'\n[project.scripts]\nchat = "app.main:run"\n',
        PYPROJECT + b'\n[build-system]\nrequires = ["hatchling"]\n',
        PYPROJECT + b'\n[tool.uv.sources]\nfastapi = { git = "https://github.com/x/fastapi" }\n',
        PYPROJECT.replace(b'"fastapi>=0.161.1",', b'"fastapi @ git+https://github.com/x/fastapi",'),
        PYPROJECT.replace(b'"fastapi>=0.161.1",', b'"fastapi @ file:///tmp/fastapi",'),
        PYPROJECT.replace(b'"pytest>=9.1.1",', b'"pytest @ https://example.com/pytest.whl",'),
        b"not = [toml",
        None,
    ],
)
def test_other_pyproject_changes_are_violations(new):
    assert python_violation(PYPROJECT, new) is True
