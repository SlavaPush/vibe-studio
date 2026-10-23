CREATE TABLE IF NOT EXISTS access_codes (
    id uuid PRIMARY KEY,
    label text NOT NULL UNIQUE,
    code_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY,
    source_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS access_code_id uuid REFERENCES access_codes (id);
CREATE INDEX IF NOT EXISTS users_access_code ON users (access_code_id);

ALTER TABLE access_codes ADD COLUMN IF NOT EXISTS used_at timestamptz;
UPDATE access_codes c SET used_at = u.created_at
FROM (SELECT access_code_id, min(created_at) AS created_at FROM users GROUP BY access_code_id) u
WHERE u.access_code_id = c.id AND c.used_at IS NULL;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    title text NOT NULL,
    title_set boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_user ON conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS assets (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    run_id uuid NOT NULL,
    message_id uuid NOT NULL,
    mime_type text NOT NULL,
    width integer,
    height integer,
    storage_path text NOT NULL,
    pending_data bytea,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY,
    position bigint GENERATED ALWAYS AS IDENTITY,
    conversation_id uuid NOT NULL REFERENCES conversations (id),
    user_id uuid NOT NULL REFERENCES users (id),
    role text NOT NULL,
    kind text,
    text text NOT NULL DEFAULT '',
    run_id uuid,
    effective_model text,
    effort text,
    asset_id uuid REFERENCES assets (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, position);

CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    conversation_id uuid NOT NULL REFERENCES conversations (id),
    source_hash text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL,
    requested_model text NOT NULL,
    effective_model text,
    effort text,
    intent text,
    complexity text,
    risk text,
    moderation_flagged boolean,
    switch_reason text,
    user_message_id uuid NOT NULL,
    assistant_message_id uuid,
    error jsonb,
    lease_token uuid,
    lease_until timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runs_idempotency UNIQUE (user_id, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active ON runs (user_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS runs_queued ON runs (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS runs_conversation ON runs (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS attempts (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id),
    purpose text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    effort text,
    status text NOT NULL,
    error_type text,
    http_status integer,
    fallback_reason text,
    input_tokens integer,
    output_tokens integer,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS attempts_run ON attempts (run_id, created_at);

CREATE TABLE IF NOT EXISTS run_events (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id),
    seq integer NOT NULL,
    type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_events_seq UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id),
    attempt_id uuid REFERENCES attempts (id),
    user_id uuid NOT NULL REFERENCES users (id),
    source_hash text NOT NULL,
    purpose text NOT NULL,
    runs integer NOT NULL DEFAULT 0,
    images integer NOT NULL DEFAULT 0,
    reserved_tokens bigint NOT NULL DEFAULT 0,
    input_tokens bigint,
    output_tokens bigint,
    state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS budget_user ON budget_reservations (user_id, created_at);
CREATE INDEX IF NOT EXISTS budget_source ON budget_reservations (source_hash, created_at);
CREATE INDEX IF NOT EXISTS budget_created ON budget_reservations (created_at);
CREATE INDEX IF NOT EXISTS budget_run ON budget_reservations (run_id);

CREATE TABLE IF NOT EXISTS quota_locks (
    name text PRIMARY KEY
);

INSERT INTO quota_locks (name) VALUES ('service') ON CONFLICT DO NOTHING;

ALTER TABLE access_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE quota_locks ENABLE ROW LEVEL SECURITY;
