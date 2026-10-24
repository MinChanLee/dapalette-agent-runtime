#!/usr/bin/env bash
# Verifies the production claim query on a real PostgreSQL:
#   1. 8 concurrent sessions drain 200 runs — every run claimed exactly once
#   2. a stale version, a terminal work and an unfinished dependency are never claimed
#   3. the partial unique indexes reject a second RUNNING run per work and a duplicate idem_key
# Usage: point the standard libpq variables (PGHOST, PGPORT, PGUSER, …) at a server, then run
#   sql/verify_postgres.sh        — it creates and drops its own scratch database.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DB="claimtest_$$"
psql -d postgres -qc "CREATE DATABASE $DB" >/dev/null
URL="$DB"
trap 'psql -d postgres -qc "DROP DATABASE $DB WITH (FORCE)" >/dev/null' EXIT

sed '/^-- Claim one PENDING run/,$d' "$DIR/postgres.sql" | psql -d "$URL" -q -v ON_ERROR_STOP=1
CLAIM="$(sed -n '/^PREPARE claim_run/,$p' "$DIR/postgres.sql")"

psql -d "$URL" -q -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO work_items (work_id, workspace_id, status, active_version)
SELECT gen_random_uuid(), gen_random_uuid(), 'CREATED', 1 FROM generate_series(1, 200);
INSERT INTO worker_runs (run_id, work_id, workspace_id, work_version, status)
SELECT gen_random_uuid(), work_id, workspace_id, 1, 'PENDING' FROM work_items;
SQL

worker() {
  { echo "$CLAIM"; for _ in $(seq 1 60); do echo "EXECUTE claim_run(gen_random_uuid(), '60 seconds');"; done; } \
    | psql -d "$URL" -qAt -v ON_ERROR_STOP=1 | grep -E '^[0-9a-f-]{36}\|' | cut -d'|' -f1
}
for i in $(seq 1 8); do worker > "/tmp/claimtest_$$_$i" & done
wait
total=$(cat /tmp/claimtest_$$_* | wc -l); unique=$(cat /tmp/claimtest_$$_* | sort -u | wc -l); rm -f /tmp/claimtest_$$_*
echo "concurrent drain: claimed $total, unique $unique (expect 200/200)"
[ "$total" -eq 200 ] && [ "$unique" -eq 200 ]

psql -d "$URL" -qAt -v ON_ERROR_STOP=1 <<SQL
TRUNCATE worker_runs, work_dependencies, work_items;
-- stale version, cancelled work, already-running work, blocked dependency, and one claimable run
INSERT INTO work_items VALUES
  ('00000000-0000-0000-0000-00000000000a', gen_random_uuid(), 'CREATED',   2, now() - interval '4 min'),
  ('00000000-0000-0000-0000-00000000000b', gen_random_uuid(), 'CANCELLED', 1, now() - interval '3 min'),
  ('00000000-0000-0000-0000-00000000000c', gen_random_uuid(), 'EXECUTING', 1, now() - interval '2 min'),
  ('00000000-0000-0000-0000-00000000000d', gen_random_uuid(), 'CREATED',   1, now() - interval '1 min'),
  ('00000000-0000-0000-0000-00000000000e', gen_random_uuid(), 'CREATED',   1, now());
INSERT INTO work_dependencies VALUES ('00000000-0000-0000-0000-00000000000d', '00000000-0000-0000-0000-00000000000c');
INSERT INTO worker_runs (run_id, work_id, workspace_id, work_version, status)
SELECT gen_random_uuid(), work_id, workspace_id, 1, 'PENDING' FROM work_items;
UPDATE worker_runs SET status = 'RUNNING' WHERE work_id = '00000000-0000-0000-0000-00000000000c';  -- c is being worked on
$CLAIM
\echo 'two claim attempts (expect exactly one row, work ...0e; a=stale version, b=cancelled, c=running, d=waits on c):'
EXECUTE claim_run(gen_random_uuid(), '60 seconds');
EXECUTE claim_run(gen_random_uuid(), '60 seconds');
SQL

echo "partial unique indexes (expect two errors):"
psql -d "$URL" -qAt -c "UPDATE worker_runs SET status = 'RUNNING' WHERE work_id = '00000000-0000-0000-0000-00000000000e' AND status = 'PENDING'; INSERT INTO worker_runs (run_id, work_id, workspace_id, work_version, status) SELECT gen_random_uuid(), work_id, workspace_id, 1, 'RUNNING' FROM work_items WHERE work_id = '00000000-0000-0000-0000-00000000000e';" 2>&1 | grep -o 'duplicate key value violates unique constraint "[a-z_]*"' || true
psql -d "$URL" -qAt -c "INSERT INTO tool_calls VALUES (gen_random_uuid(), gen_random_uuid(), 't', 'k1', 'SUCCEEDED', '{}'), (gen_random_uuid(), gen_random_uuid(), 't', 'k1', 'REQUESTED', '{}');" 2>&1 | grep -o 'duplicate key value violates unique constraint "[a-z_]*"' || true
psql -d "$URL" -qAt -c "INSERT INTO tool_calls VALUES (gen_random_uuid(), gen_random_uuid(), 't', NULL, 'CANCELLED', '{}'), (gen_random_uuid(), gen_random_uuid(), 't', NULL, 'CANCELLED', '{}'); SELECT 'NULL idem_keys coexist: ok';"
