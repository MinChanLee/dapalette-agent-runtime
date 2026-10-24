"""Worker — claims runs with a lease, keeps it alive with a heartbeat, and gives work back on crash.

A run is claimable only if all of these hold, checked atomically in one UPDATE … RETURNING:
  * it is PENDING and unowned
  * its version is the work's active version (an older version is stale: the owner said something newer)
  * the work is not terminal — a leftover run must never resurrect CANCELLED work
    (seen in dev: a run left behind by a July cancellation claimed a month later and acted in the real world)
  * no other run of the same work is RUNNING (also enforced by a partial unique index)
  * every dependency work is COMPLETED (not merely "had a successful run once")
Oldest work first: ordering by random run id starved some work for over an hour under load.

A RUNNING run whose lease expired is re-claimable by another worker (the previous one died).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .db import append_event, now

LEASE_SECONDS = 60.0


@dataclass(frozen=True)
class Run:
    run_id: str
    work_id: str
    workspace_id: str
    work_version: int


_CANDIDATE = """
    SELECT r.run_id FROM worker_runs r
    JOIN work_items w ON w.work_id = r.work_id
    WHERE {status_clause}
      AND w.active_version = r.work_version
      AND w.status NOT IN ('CANCELLED', 'COMPLETED', 'FAILED')
      AND NOT EXISTS (
          SELECT 1 FROM worker_runs other
          WHERE other.work_id = r.work_id AND other.status = 'RUNNING' AND other.run_id <> r.run_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM work_dependencies d JOIN work_items dep ON dep.work_id = d.depends_on_work_id
          WHERE d.work_id = r.work_id AND dep.status <> 'COMPLETED'
      )
    ORDER BY w.created_at, r.run_id
    LIMIT 1
"""


def _claim(conn: sqlite3.Connection, worker_id: str, status_clause: str, params: tuple[object, ...], lease: float) -> Run | None:
    ts = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"""UPDATE worker_runs
                SET status = 'RUNNING', owner_worker_id = ?, lease_expires_at = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE run_id = ({_CANDIDATE.format(status_clause=status_clause)})
                RETURNING run_id, work_id, workspace_id, work_version""",
            (worker_id, ts + lease, ts, ts, *params),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE work_items SET status = 'EXECUTING' WHERE work_id = ? AND active_version = ? AND status <> 'EXECUTING'",
                (row["work_id"], row["work_version"]),
            )
            append_event(conn, row["workspace_id"], "CLAIM_WORKER", row["run_id"], {"worker_id": worker_id})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return Run(row["run_id"], row["work_id"], row["workspace_id"], row["work_version"]) if row else None


def claim(conn: sqlite3.Connection, worker_id: str, lease: float = LEASE_SECONDS) -> Run | None:
    """Claim one fresh PENDING run, or else re-claim one whose lease expired."""
    fresh = _claim(conn, worker_id, "r.status = 'PENDING' AND r.owner_worker_id IS NULL", (), lease)
    if fresh is not None:
        return fresh
    return _claim(conn, worker_id, "r.status = 'RUNNING' AND r.lease_expires_at < ?", (now(),), lease)


def renew_lease(conn: sqlite3.Connection, run: Run, worker_id: str, lease: float = LEASE_SECONDS) -> bool:
    """Heartbeat. Returns False if the lease was lost (someone else re-claimed it) — the caller must stop."""
    ts = now()
    cur = conn.execute(
        "UPDATE worker_runs SET lease_expires_at = ?, heartbeat_at = ? WHERE run_id = ? AND owner_worker_id = ? AND status = 'RUNNING'",
        (ts + lease, ts, run.run_id, worker_id),
    )
    return cur.rowcount == 1


def stop_reason(conn: sqlite3.Connection, run: Run) -> str | None:
    """Why this run must stop now, if at all: superseded by a newer version, or cancelled.
    Checked between agent steps and inside every tool call's checklist."""
    work = conn.execute("SELECT active_version FROM work_items WHERE work_id = ?", (run.work_id,)).fetchone()
    if work is None or work["active_version"] != run.work_version:
        return "superseded by a newer version of this work"
    if conn.execute("SELECT 1 FROM cancellations WHERE work_id = ? AND status = 'REQUESTED'", (run.work_id,)).fetchone():
        return "cancelled by the owner"
    return None


def finish(conn: sqlite3.Connection, run: Run, worker_id: str, outcome: str, *, pause_for_approval: bool = False) -> None:
    """outcome: SUCCEEDED | FAILED | CANCELLED. Only the lease holder can finish its run."""
    ts = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """UPDATE worker_runs SET status = ?, ended_at = ?, owner_worker_id = NULL, lease_expires_at = NULL
               WHERE run_id = ? AND owner_worker_id = ? AND status = 'RUNNING'""",
            (outcome, ts, run.run_id, worker_id),
        )
        if cur.rowcount == 1:
            cancelled = conn.execute(
                "UPDATE cancellations SET status = 'APPLIED', applied_at = ? WHERE work_id = ? AND status = 'REQUESTED'",
                (ts, run.work_id),
            ).rowcount
            if cancelled:
                work_status = "CANCELLED"
            elif outcome == "CANCELLED":
                work_status = None  # superseded: the newer version's run owns the work's status
            elif pause_for_approval:
                work_status = "PAUSED"
            else:
                work_status = {"SUCCEEDED": "COMPLETED", "FAILED": "FAILED"}[outcome]
            if work_status is not None:
                conn.execute(
                    "UPDATE work_items SET status = ? WHERE work_id = ? AND active_version = ?",
                    (work_status, run.work_id, run.work_version),
                )
            append_event(conn, run.workspace_id, "RUN_FINISHED", run.run_id, {"outcome": outcome, "work_status": work_status})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class Heartbeat:
    """Renews the lease on a background thread while the agent works; sets ``lost`` if renewal fails."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], run: Run, worker_id: str, interval: float, lease: float) -> None:
        self._conn_factory, self._run, self._worker_id = conn_factory, run, worker_id
        self._interval, self._lease = interval, lease
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        conn = self._conn_factory()
        try:
            while not self._stop.wait(self._interval):
                if not renew_lease(conn, self._run, self._worker_id, self._lease):
                    self.lost.set()
                    return
        finally:
            conn.close()
