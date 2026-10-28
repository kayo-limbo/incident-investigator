CREATE TABLE IF NOT EXISTS investigation_tasks (
    id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    request jsonb NOT NULL,
    request_hash text NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    worker_id text,
    run_token uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    result jsonb,
    error_code text,
    CHECK ((state = 'queued' AND run_token IS NULL AND started_at IS NULL AND finished_at IS NULL)
        OR (state = 'running' AND run_token IS NOT NULL AND worker_id IS NOT NULL
            AND started_at IS NOT NULL AND finished_at IS NULL)
        OR (state IN ('completed', 'failed', 'cancelled') AND finished_at IS NOT NULL)),
    CHECK (state <> 'completed' OR result IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS investigation_tasks_queue
    ON investigation_tasks (created_at, id) WHERE state = 'queued';
