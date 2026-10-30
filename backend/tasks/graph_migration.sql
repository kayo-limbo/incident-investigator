CREATE TABLE IF NOT EXISTS task_graph_runs (
    task_id uuid PRIMARY KEY REFERENCES investigation_tasks(id) ON DELETE CASCADE,
    protocol text NOT NULL,
    request_hash text NOT NULL,
    deadline timestamptz NOT NULL,
    inflight_turn integer
);
CREATE TABLE IF NOT EXISTS task_graph_checkpoints (
    task_id uuid NOT NULL REFERENCES task_graph_runs(task_id) ON DELETE CASCADE,
    checkpoint_id text NOT NULL,
    position bigint GENERATED ALWAYS AS IDENTITY,
    parent_id text,
    checkpoint jsonb NOT NULL,
    metadata jsonb NOT NULL,
    PRIMARY KEY(task_id, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS task_graph_latest ON task_graph_checkpoints(task_id, position DESC);
