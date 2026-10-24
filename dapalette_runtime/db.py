"""Storage: schema and connection.

The excerpt runs on SQLite (stdlib) so it is runnable anywhere. Production runs on PostgreSQL;
the only dialect-specific piece that matters — the worker claim query with
``FOR UPDATE SKIP LOCKED`` — is in ``sql/postgres.sql`` and verified against a real Postgres.

Invariants live in the schema, not only in code:
  * one RUNNING run per work           — partial unique index
  * one PENDING run per (work, version) — partial unique index (concurrent reuse can't double-enqueue)
  * one live tool call per idem_key     — partial unique index (duplicate side effects are impossible,
                                          not just unlikely)
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_store (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    source_id     TEXT,
    payload       TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id            TEXT PRIMARY KEY,
    workspace_id           TEXT NOT NULL,
    name                   TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('PLANNING', 'ACTIVE', 'CLOSED')),
    brief                  TEXT NOT NULL,          -- product, KPI, budget, end date (JSON)
    approval_needed_tools  TEXT NOT NULL,          -- JSON array, owner-toggleable
    created_at             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS creators (
    creator_id    TEXT PRIMARY KEY,
    handle        TEXT NOT NULL,
    platform      TEXT NOT NULL,
    followers     INTEGER NOT NULL,
    category      TEXT NOT NULL,
    email         TEXT NOT NULL
);

-- per (campaign, creator) collaboration ledger: what was agreed and what happened
CREATE TABLE IF NOT EXISTS collaborations (
    campaign_id   TEXT NOT NULL,
    creator_id    TEXT NOT NULL,
    stage         TEXT NOT NULL,   -- CONTACTED → CONTRACTED → SHIPPED → POSTED → SETTLED
    fee_krw       INTEGER,
    tracking_no   TEXT,
    post_url      TEXT,
    paid_krw      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (campaign_id, creator_id)
);

CREATE TABLE IF NOT EXISTS outbox (               -- stand-in for real email / payment providers
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    work_id         TEXT PRIMARY KEY,
    workspace_id    TEXT NOT NULL,
    campaign_id     TEXT,
    thread_key      TEXT NOT NULL,     -- owner thread, or campaign:creator for creator mail
    trigger         TEXT NOT NULL CHECK (trigger IN ('OWNER', 'CREATOR', 'SCHEDULER')),
    status          TEXT NOT NULL CHECK (status IN ('CREATED', 'EXECUTING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED')),
    active_version  INTEGER NOT NULL,
    description     TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS work_versions (
    work_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    source_type   TEXT NOT NULL,
    source_ref    TEXT,
    created_at    REAL NOT NULL,
    PRIMARY KEY (work_id, version)
);

CREATE TABLE IF NOT EXISTS work_dependencies (
    work_id             TEXT NOT NULL,
    depends_on_work_id  TEXT NOT NULL,
    PRIMARY KEY (work_id, depends_on_work_id)
);

CREATE TABLE IF NOT EXISTS worker_runs (
    run_id           TEXT PRIMARY KEY,
    work_id          TEXT NOT NULL,
    workspace_id     TEXT NOT NULL,
    work_version     INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    owner_worker_id  TEXT,
    lease_expires_at REAL,
    heartbeat_at     REAL,
    started_at       REAL,
    ended_at         REAL,
    created_at       REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_runs_one_running
    ON worker_runs (work_id) WHERE status = 'RUNNING';
CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_runs_pending_per_version
    ON worker_runs (work_id, work_version) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS cancellations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('REQUESTED', 'APPLIED')),
    requested_at  REAL NOT NULL,
    applied_at    REAL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id  TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    work_id       TEXT,
    run_id        TEXT,
    tool_name     TEXT NOT NULL,
    idem_key      TEXT,
    status        TEXT NOT NULL CHECK (status IN ('REQUESTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    input_data    TEXT NOT NULL,
    output_data   TEXT,
    error_log     TEXT,
    created_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tool_calls_idem_key
    ON tool_calls (idem_key) WHERE idem_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    work_id       TEXT,
    tool_name     TEXT NOT NULL,
    idem_key      TEXT NOT NULL,      -- the exact key the runner will compute for the gated call
    status        TEXT NOT NULL CHECK (status IN ('REQUESTED', 'APPROVED', 'REJECTED', 'EXPIRED')),
    payload       TEXT NOT NULL,      -- what the owner sees on the card
    expires_at    REAL NOT NULL,
    decided_at    REAL
);
CREATE INDEX IF NOT EXISTS ix_approvals_idem ON approvals (idem_key);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Autocommit connection; transactions are opened explicitly with ``BEGIN IMMEDIATE`` where needed."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30, uri=path.startswith("file:"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if "memory" not in path:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def now() -> float:
    return time.time()


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def append_event(conn: sqlite3.Connection, workspace_id: str, event_type: str, source_id: str | None, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO event_store (workspace_id, event_type, source_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, event_type, source_id, dumps(payload), now()),
    )
