CREATE TABLE IF NOT EXISTS scheduler (
    id INTEGER PRIMARY KEY,
    next_event_id BIGINT NOT NULL,
    active_release_id TEXT,
    release_blocked_by TEXT,
    published_sha TEXT,
    external_wait_since TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    access_code_id TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants (id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_participant ON sessions (participant_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES participants (id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    profile TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rejection_reason TEXT,
    spec TEXT,
    branch TEXT,
    base_sha TEXT,
    pr_url TEXT,
    changed_files TEXT NOT NULL,
    fix_cycles INTEGER NOT NULL,
    model_calls INTEGER NOT NULL,
    model_calls_limit INTEGER NOT NULL,
    input_tokens BIGINT NOT NULL,
    output_tokens BIGINT NOT NULL,
    event_seq INTEGER NOT NULL,
    pr_number INTEGER,
    head_sha TEXT,
    failure_kind TEXT,
    checks TEXT,
    merge_sha TEXT,
    pr_review_calls INTEGER NOT NULL DEFAULT 0,
    rework_count INTEGER NOT NULL DEFAULT 0,
    rework_reason TEXT,
    rework_priority BOOLEAN NOT NULL DEFAULT false,
    rework_context TEXT,
    rework_of TEXT,
    change_class TEXT NOT NULL DEFAULT 'frontend_ui',
    contour TEXT,
    owner_review TEXT,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    halt_reason TEXT
);

CREATE INDEX IF NOT EXISTS tasks_status_created ON tasks (status, created_at);
CREATE INDEX IF NOT EXISTS tasks_owner_created ON tasks (owner_id, created_at);

CREATE TABLE IF NOT EXISTS task_events (
    event_id BIGINT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    role TEXT,
    tool TEXT,
    artifact TEXT,
    payload TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants (id),
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    object_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (participant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS release_candidates (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    expected_previous_sha TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    suite_hash TEXT NOT NULL,
    ci_run_ids TEXT NOT NULL,
    pr_url TEXT,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    is_published BOOLEAN NOT NULL,
    deploy TEXT,
    pr_review_id TEXT,
    e2e TEXT,
    superseded_by TEXT,
    origin TEXT NOT NULL DEFAULT 'task',
    origin_decision_id TEXT,
    blocked_reason TEXT,
    verify TEXT,
    rollback TEXT,
    revert TEXT,
    reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS release_candidates_live_commit ON release_candidates (task_id, commit_sha)
WHERE kind = 'release' AND status <> 'revoked';

CREATE TABLE IF NOT EXISTS pr_reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    head_sha TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reasons TEXT NOT NULL,
    required_changes TEXT NOT NULL,
    checklist TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner_summary TEXT,
    UNIQUE (task_id, head_sha)
);

CREATE TABLE IF NOT EXISTS check_reports (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    created_at TEXT NOT NULL,
    commit_sha TEXT,
    spec_hash TEXT,
    suite_hash TEXT,
    checks TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks (id),
    release_id TEXT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    external_ref TEXT,
    detail TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS operations_task ON operations (task_id);

CREATE TABLE IF NOT EXISTS intake_reservations (
    id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants (id),
    created_at TEXT NOT NULL
);

INSERT INTO scheduler (id, next_event_id)
SELECT 1, 1
WHERE NOT EXISTS (SELECT 1 FROM scheduler WHERE id = 1);
