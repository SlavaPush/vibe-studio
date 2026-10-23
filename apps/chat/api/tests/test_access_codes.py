import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from conftest import (
    DEMO_CODE,
    HANG,
    SCHEMA,
    FakeProviders,
    FakeStorage,
    User,
    add_code,
    client_for,
    login,
    make_app,
    sql,
)

from app.config import Limits

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "access_codes.py"
CODE_A = "aaaa-bbbb-cccc"
CODE_B = "dddd-eeee-ffff"


def revoke(db, label: str) -> None:
    sql(db, "UPDATE access_codes SET revoked_at = now() WHERE label = %s", (label,))


def wait_for(condition, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "условие не выполнилось"
        time.sleep(0.02)


def earlier_user(client, db, label: str) -> User:
    user_id, token = str(uuid.uuid4()), uuid.uuid4().hex
    sql(
        db,
        "INSERT INTO users (id, source_hash, access_code_id) SELECT %s, 'earlier', id FROM access_codes WHERE label = %s",
        (user_id, label),
    )
    sql(
        db,
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (encode(sha256(%s::bytea), 'hex'), %s, 'csrf', now() + interval '1 day')",
        (token, user_id),
    )
    return User(client, token, "csrf", user_id)


def test_code_is_single_use_and_first_session_keeps_working(db):
    add_code(db, "a", CODE_A)
    add_code(db, "b", CODE_B)
    revoke(db, "b")
    with client_for(make_app(db)) as client:
        first = login(client, CODE_A)
        for code in (CODE_A, "AAAA BBBB CCCC", CODE_B):
            response = client.post("/api/demo-session", json={"code": code})
            assert response.status_code == 401
            assert response.json()["code"] == "invalid_demo_code"
        assert first.get("/api/me").status_code == 200
        assert first.ask("Привет")["status"] == "completed"
    assert sql(db, "SELECT label, used_at IS NOT NULL FROM access_codes WHERE label IN ('a', 'b') ORDER BY label") == [
        ("a", True),
        ("b", False),
    ]
    assert sql(db, "SELECT count(*) FROM users") == [(1,)]


def test_parallel_logins_with_one_code_create_one_session(db):
    add_code(db, "a", CODE_A)
    with client_for(make_app(db)) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: client.post("/api/demo-session", json={"code": CODE_A}), range(8)))
    assert sorted(response.status_code for response in responses) == [201] + [401] * 7
    assert sql(db, "SELECT count(*) FROM users") == [(1,)]
    assert sql(db, "SELECT count(*) FROM sessions") == [(1,)]


def test_login_accepts_active_code_in_any_format_and_rejects_unknown_or_revoked(db):
    add_code(db, "a", CODE_A)
    with client_for(make_app(db)) as client:
        assert login(client, " AAAA bbbb-CCCC ").id
        revoke(db, "a")
        for code in (CODE_A, "unknown-code"):
            response = client.post("/api/demo-session", json={"code": code})
            assert response.status_code == 401
            assert response.json()["code"] == "invalid_demo_code"
    owners = sql(db, "SELECT c.label FROM users u JOIN access_codes c ON c.id = u.access_code_id")
    assert owners == [("a",)]


def test_rate_limit_applies_before_code_check(db):
    with client_for(make_app(db, sessions_per_hour=2)) as client:
        assert client.post("/api/demo-session", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/demo-session", json={"code": "wrong"}).status_code == 401
        limited = client.post("/api/demo-session", json={"code": DEMO_CODE})
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"


def test_revoking_code_closes_its_sessions_only(db):
    add_code(db, "a", CODE_A)
    add_code(db, "b", CODE_B)
    providers, storage = FakeProviders(), FakeStorage()
    with client_for(make_app(db, providers, storage)) as client:
        alice, bob = login(client, CODE_A), login(client, CODE_B)
        conversation_id = alice.conversation()
        run = alice.ask("Нарисуй кота", conversation_id=conversation_id)
        asset_id = alice.get(f"/api/conversations/{conversation_id}/messages").json()["items"][-1]["asset"]["id"]
        assert alice.get(f"/api/assets/{asset_id}/url").status_code == 200

        revoke(db, "a")
        responses = [
            alice.get("/api/me"),
            alice.post("/api/conversations", {"title": None}),
            alice.send(conversation_id, "Привет"),
            alice.get(f"/api/runs/{run['id']}/events"),
            alice.get(f"/api/assets/{asset_id}/url"),
        ]
        assert [(response.status_code, response.json()["code"]) for response in responses] == [(401, "unauthorized")] * 5

        assert bob.get("/api/me").status_code == 200
        assert bob.ask("Привет")["status"] == "completed"
        again = client.post("/api/demo-session", json={"code": CODE_A})
        assert again.status_code == 401
        assert again.json()["code"] == "invalid_demo_code"


def test_open_event_stream_closes_after_revocation(db):
    add_code(db, "a", CODE_A)
    add_code(db, "b", CODE_B)
    providers = FakeProviders()
    providers.fail("text", HANG, HANG)
    with client_for(make_app(db, providers)) as client:
        alice, bob = login(client, CODE_A), login(client, CODE_B)
        run_a = alice.send(alice.conversation(), "Привет").json()["id"]
        run_b = bob.send(bob.conversation(), "Привет").json()["id"]
        wait_for(lambda: providers.count("text") == 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stream_a = pool.submit(alice.events, run_a)
            stream_b = pool.submit(bob.events, run_b)
            time.sleep(1.0)
            revoke(db, "a")
            events_a = stream_a.result(timeout=5)
            assert events_a and events_a[0]["type"] == "accepted"
            assert sql(db, "SELECT status FROM runs WHERE id = %s", (run_a,)) == [("running",)]
            sql(
                db,
                """
                INSERT INTO run_events (id, run_id, seq, type, payload)
                SELECT %s, %s, max(seq) + 1, 'failed', '{}'::jsonb FROM run_events WHERE run_id = %s
                """,
                (str(uuid.uuid4()), run_b, run_b),
            )
            events_b = stream_b.result(timeout=5)
        assert events_b[-1]["type"] == "failed"


def test_participant_quota_follows_code_across_sessions_and_restart(db):
    add_code(db, "a", CODE_A)
    add_code(db, "b", CODE_B)
    settings = dict(user_limits=Limits(1, 5, 10_000_000))
    with client_for(make_app(db, **settings)) as client:
        login(client, CODE_A).ask("Привет")
        second = earlier_user(client, db, "a")
        response = second.send(second.conversation(), "Привет")
        assert response.status_code == 429
        assert response.json()["code"] == "quota_exceeded"
    with client_for(make_app(db, **settings)) as client:
        third = earlier_user(client, db, "a")
        assert third.send(third.conversation(), "Привет").status_code == 429
        other = login(client, CODE_B)
        assert other.send(other.conversation(), "Привет").status_code == 202


def test_parallel_sessions_of_one_code_do_not_exceed_participant_limit(db):
    add_code(db, "a", CODE_A)
    with client_for(make_app(db, user_limits=Limits(3, 5, 10_000_000))) as client:
        users = [login(client, CODE_A)] + [earlier_user(client, db, "a") for _ in range(5)]
        conversations = [user.conversation() for user in users]
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda pair: pair[0].send(pair[1], "Привет"), zip(users, conversations)))
        accepted = [response for response in responses if response.status_code == 202]
        assert len(accepted) == 3
        assert {response.json()["code"] for response in responses if response.status_code != 202} == {"quota_exceeded"}
        for user, response in zip(users, responses):
            if response.status_code == 202:
                user.wait(response.json()["id"])


def test_executor_images_and_tokens_count_toward_code(db):
    add_code(db, "a", CODE_A)
    with client_for(make_app(db, user_limits=Limits(60, 1, 10_000_000))) as client:
        assert login(client, CODE_A).ask("Нарисуй кота")["status"] == "completed"
        run = earlier_user(client, db, "a").ask("Нарисуй собаку")
        assert run["status"] == "failed"
        assert run["error"]["code"] == "quota_exceeded"

    used = sql(
        db,
        "SELECT sum(CASE WHEN state = 'settled' THEN input_tokens + output_tokens ELSE reserved_tokens END) FROM budget_reservations",
    )[0][0]
    providers = FakeProviders()
    with client_for(make_app(db, providers, user_limits=Limits(60, 5, used + 10))) as client:
        run = earlier_user(client, db, "a").ask("Привет")
        assert run["status"] == "failed"
        assert run["error"]["code"] == "quota_exceeded"
        assert providers.count("classifier") == 0


def test_session_of_user_without_code_is_rejected(db):
    user_id, token = str(uuid.uuid4()), "legacy-token"
    sql(db, "INSERT INTO users (id, source_hash) VALUES (%s, 'legacy')", (user_id,))
    sql(
        db,
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (encode(sha256(%s::bytea), 'hex'), %s, 'c', now() + interval '1 day')",
        (token, user_id),
    )
    with client_for(make_app(db)) as client:
        response = client.get("/api/me", headers={"Cookie": f"vc_session={token}"})
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"


def test_codes_are_stored_hashed_with_row_level_security(db):
    add_code(db, "a", CODE_A)
    rows = sql(db, "SELECT row_to_json(c)::text FROM access_codes c")
    assert all(code not in row[0] and code.replace("-", "") not in row[0] for row in rows for code in (CODE_A, DEMO_CODE))
    assert sql(db, "SELECT relrowsecurity FROM pg_class WHERE relname = 'access_codes'") == [(True,)]


def test_schema_upgrades_p3_database_without_data_loss(db):
    params = "-c search_path=upgrade"
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS upgrade CASCADE")
        conn.execute("CREATE SCHEMA upgrade")
    try:
        with psycopg.connect(db, autocommit=True, options=params) as conn:
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
            conn.execute("ALTER TABLE users DROP COLUMN access_code_id")
            conn.execute("DROP TABLE access_codes")
            user_id = str(uuid.uuid4())
            conn.execute("INSERT INTO users (id, source_hash) VALUES (%s, 'p3')", (user_id,))
            conn.execute("INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, 'Диалог P3')", (str(uuid.uuid4()), user_id))
            for _ in range(2):
                conn.execute(SCHEMA.read_text(encoding="utf-8"))
            assert conn.execute("SELECT id::text, access_code_id FROM users").fetchall() == [(user_id, None)]
            assert conn.execute("SELECT title FROM conversations").fetchall() == [("Диалог P3",)]
            assert conn.execute("SELECT count(*) FROM access_codes").fetchone() == (0,)
    finally:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute("DROP SCHEMA upgrade CASCADE")


def test_schema_marks_codes_with_users_as_used(db):
    params = "-c search_path=upgrade"
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS upgrade CASCADE")
        conn.execute("CREATE SCHEMA upgrade")
    try:
        with psycopg.connect(db, autocommit=True, options=params) as conn:
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
            conn.execute("ALTER TABLE access_codes DROP COLUMN used_at")
            codes = {label: str(uuid.uuid4()) for label in ("a", "b")}
            for label, code_id in codes.items():
                conn.execute(
                    "INSERT INTO access_codes (id, label, code_hash) VALUES (%s, %s, %s)", (code_id, label, label)
                )
            for created_at in ("2026-10-24 10:00+00", "2026-10-24 09:00+00"):
                conn.execute(
                    "INSERT INTO users (id, source_hash, access_code_id, created_at) VALUES (%s, 'p4', %s, %s)",
                    (str(uuid.uuid4()), codes["a"], created_at),
                )
            conn.execute("SET TIME ZONE 'UTC'")
            used = "SELECT label, used_at::text FROM access_codes ORDER BY label"
            expected = [("a", "2026-10-24 09:00:00+00"), ("b", None)]
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
            assert conn.execute(used).fetchall() == expected
            conn.execute(
                "INSERT INTO users (id, source_hash, access_code_id, created_at) VALUES (%s, 'p4', %s, '2026-10-24 08:00+00')",
                (str(uuid.uuid4()), codes["a"]),
            )
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
            assert conn.execute(used).fetchall() == expected
    finally:
        with psycopg.connect(db, autocommit=True) as conn:
            conn.execute("DROP SCHEMA upgrade CASCADE")


def manage(db, *args: str) -> tuple[int, list[dict], str]:
    env = {**os.environ, "DATABASE_URL": db}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.returncode, lines, result.stdout + result.stderr


def test_management_script_adds_lists_and_revokes_codes(db):
    code, lines, _ = manage(db, "add", "alice")
    assert code == 0
    issued = lines[0]["code"]
    assert len(issued.replace("-", "")) == 12
    assert manage(db, "add", "alice")[0] == 1

    with client_for(make_app(db)) as client:
        alice, other = login(client, issued), login(client, DEMO_CODE)
        code, lines, output = manage(db, "list")
        assert code == 0
        assert issued not in output and issued.replace("-", "") not in output
        assert db not in output
        listed = {line["label"]: line for line in lines}
        assert listed["alice"]["users"] == 1
        assert listed["alice"]["active_sessions"] == 1
        assert listed["alice"]["revoked_at"] is None
        assert listed["alice"]["used_at"] is not None
        assert listed["demo"]["used_at"] is not None

        code, lines, output = manage(db, "revoke", "alice")
        assert code == 0 and lines[0]["revoked_at"]
        assert db not in output
        assert alice.get("/api/me").status_code == 401
        assert client.post("/api/demo-session", json={"code": issued}).status_code == 401
        listed = {line["label"]: line for line in manage(db, "list")[1]}
        assert listed["alice"]["active_sessions"] == 0
        assert listed["alice"]["users"] == 1
        assert listed["demo"]["active_sessions"] == 1
        assert manage(db, "revoke", "alice")[0] == 0
        assert manage(db, "revoke", "nobody")[0] == 1
        assert other.get("/api/me").status_code == 200
