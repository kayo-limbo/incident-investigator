CREATE TABLE IF NOT EXISTS task_events (
    task_id uuid NOT NULL REFERENCES investigation_tasks(id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence > 0),
    kind text NOT NULL CHECK (kind IN ('state_changed', 'snapshot')),
    state text NOT NULL,
    attempt_count integer NOT NULL,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (task_id, sequence)
);

-- 迁移只能确认当前状态，不能补造过去的状态变化。
INSERT INTO task_events (task_id, sequence, kind, state, attempt_count, error_code)
SELECT id, 1, 'snapshot', state, attempt_count, error_code FROM investigation_tasks
ON CONFLICT (task_id, sequence) DO NOTHING;

CREATE OR REPLACE FUNCTION record_task_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;
    -- INSERT/UPDATE已持有当前任务行锁，同一任务的编号按事务提交串行化。
    INSERT INTO task_events (task_id, sequence, kind, state, attempt_count, error_code)
    SELECT NEW.id, COALESCE(MAX(sequence), 0) + 1, 'state_changed',
           NEW.state, NEW.attempt_count, NEW.error_code
    FROM task_events WHERE task_id = NEW.id;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS task_state_event ON investigation_tasks;
CREATE TRIGGER task_state_event AFTER INSERT OR UPDATE OF state ON investigation_tasks
FOR EACH ROW EXECUTE FUNCTION record_task_state();
