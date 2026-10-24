from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any

import dapalette_runtime  # noqa: F401  (registers tools)
from dapalette_runtime import control_plane as cp
from dapalette_runtime.db import connect, dumps, new_id, now
from dapalette_runtime.tools import approvals
from dapalette_runtime.worker import Run, claim

WS = "ws-test"


def file_db() -> tuple[str, sqlite3.Connection]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path, connect(path)


def mem_db() -> sqlite3.Connection:
    return connect(":memory:")


def seed_campaign(conn: sqlite3.Connection, *, status: str = "ACTIVE", budget: int = 3_000_000, approval_tools: list[str] | None = None) -> str:
    campaign_id = new_id()
    conn.execute(
        "INSERT INTO campaigns VALUES (?, ?, 'c', ?, ?, ?, ?)",
        (
            campaign_id,
            WS,
            status,
            dumps({"product": "p", "kpi_posts": 2, "budget_krw": budget, "end_date": "2026-12-31"}),
            dumps(approval_tools if approval_tools is not None else list(approvals.DEFAULT_APPROVAL_TOOLS)),
            now(),
        ),
    )
    for i in range(3):
        conn.execute("INSERT OR IGNORE INTO creators VALUES (?, ?, 'instagram', ?, 'beauty', ?)", (f"cr{i}", f"h{i}", 100_000 - i, f"h{i}@x.com"))
    return campaign_id


def owner_work(conn: sqlite3.Connection, thread: str = "t-1", text: str = "@AI do it") -> cp.Enqueued:
    enq = cp.owner_message(conn, workspace_id=WS, thread_key=thread, text=text)
    assert enq is not None
    return enq


def claimed_run(conn: sqlite3.Connection, worker: str = "w1") -> Run:
    owner_work(conn)
    run = claim(conn, worker)
    assert run is not None
    return run


def approve_all(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT approval_id FROM approvals WHERE status = 'REQUESTED'").fetchall():
        approvals.decide(conn, row["approval_id"], True)


def rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
