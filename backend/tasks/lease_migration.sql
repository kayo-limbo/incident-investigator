ALTER TABLE investigation_tasks ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE investigation_tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0);
UPDATE investigation_tasks SET attempt_count=1 WHERE run_token IS NOT NULL AND attempt_count=0;
-- 旧 Worker 没有租约协议，不能安全地与新版同时运行。升级前必须停旧 Worker。
UPDATE investigation_tasks SET state='failed', error_code='legacy_running_without_lease',
    finished_at=clock_timestamp() WHERE state='running' AND lease_expires_at IS NULL;
CREATE INDEX IF NOT EXISTS investigation_tasks_expired
    ON investigation_tasks (lease_expires_at, id) WHERE state='running';
