-- Production (PostgreSQL) form of the worker queue.
-- Same invariants as the SQLite schema in dapalette_runtime/db.py; the claim adds FOR UPDATE SKIP LOCKED
-- so concurrent workers each take a different row instead of queueing on the same one.

CREATE TABLE work_items (
    work_id         uuid PRIMARY KEY,
    workspace_id    uuid NOT NULL,
    status          text NOT NULL CHECK (status IN ('CREATED', 'EXECUTING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED')),
    active_version  integer NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE work_dependencies (
    work_id             uuid NOT NULL REFERENCES work_items,
    depends_on_work_id  uuid NOT NULL REFERENCES work_items,
    PRIMARY KEY (work_id, depends_on_work_id)
);

CREATE TABLE worker_runs (
    run_id           uuid PRIMARY KEY,
    work_id          uuid NOT NULL REFERENCES work_items,
    workspace_id     uuid NOT NULL,
    work_version     integer NOT NULL,
    status           text NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    owner_worker_id  uuid,
    lease_expires_at timestamptz,
    heartbeat_at     timestamptz,
    started_at       timestamptz,
    ended_at         timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_worker_runs_one_running ON worker_runs (work_id) WHERE status = 'RUNNING';
CREATE UNIQUE INDEX ux_worker_runs_pending_per_version ON worker_runs (work_id, work_version) WHERE status = 'PENDING';

CREATE TABLE tool_calls (
    tool_call_id  uuid PRIMARY KEY,
    workspace_id  uuid NOT NULL,
    tool_name     text NOT NULL,
    idem_key      text,
    status        text NOT NULL,
    input_data    jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_tool_calls_idem_key ON tool_calls (idem_key) WHERE idem_key IS NOT NULL;

-- Claim one PENDING run ($1 = worker id, $2 = lease interval). Atomic: one UPDATE … RETURNING.
-- Stale re-claim is the same statement with `status = 'RUNNING' AND lease_expires_at < now()`.
PREPARE claim_run(uuid, interval) AS
UPDATE worker_runs
SET status = 'RUNNING', owner_worker_id = $1, lease_expires_at = now() + $2, heartbeat_at = now(),
    started_at = COALESCE(started_at, now())
WHERE run_id IN (
    SELECT r.run_id FROM worker_runs r
    JOIN work_items w ON w.work_id = r.work_id
    WHERE r.status = 'PENDING' AND r.owner_worker_id IS NULL
      AND w.active_version = r.work_version
      AND w.status NOT IN ('CANCELLED', 'COMPLETED', 'FAILED')
      AND NOT EXISTS (SELECT 1 FROM worker_runs o WHERE o.work_id = r.work_id AND o.status = 'RUNNING')
      AND NOT EXISTS (
          SELECT 1 FROM work_dependencies d JOIN work_items dep ON dep.work_id = d.depends_on_work_id
          WHERE d.work_id = r.work_id AND dep.status <> 'COMPLETED'
      )
    ORDER BY w.created_at, r.run_id
    LIMIT 1
    FOR UPDATE OF r SKIP LOCKED
)
RETURNING run_id, work_id, work_version;
