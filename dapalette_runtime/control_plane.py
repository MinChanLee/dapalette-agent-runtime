"""Control Plane — turns inbound events into versioned work. Deterministic: no LLM call here.

An early version asked an LLM to route every inbound message (respond now? cancel? which campaign?).
Routing decisions made by a model were unreproducible and occasionally attached work to the wrong
campaign, so the Control Plane became pure code with one question:

    "Is there already live work for this conversation?"
       yes → bump its version (the worker drops the stale run and restarts with the new message in context)
       no  → create work, version 1

Execution — including writing the reply — belongs to the worker.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .db import append_event, new_id, now

_AI_MENTION = re.compile(r"(?:^|[^A-Za-z0-9_])@(?:ai|mainai)\b", re.IGNORECASE)
_LIVE = ("CREATED", "EXECUTING", "PAUSED")


@dataclass(frozen=True)
class Enqueued:
    work_id: str
    version: int
    reused: bool


def owner_message(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    thread_key: str,
    text: str,
    campaign_id: str | None = None,
    ui_trigger: bool = False,
) -> Enqueued | None:
    """Owner chat → work. Chat only reaches the agent when it mentions @AI (people also talk to each other
    in these threads); UI buttons are explicit triggers."""
    message_id = new_id()
    append_event(conn, workspace_id, "OWNER_MESSAGE_RECEIVED", message_id, {"thread_key": thread_key, "text": text, "campaign_id": campaign_id})
    if not ui_trigger and not _AI_MENTION.search(text):
        return None
    return _create_or_reuse(
        conn,
        workspace_id=workspace_id,
        thread_key=thread_key,
        trigger="OWNER",
        campaign_id=campaign_id,
        source_ref=message_id,
        description=text[:200],
    )


def creator_reply(conn: sqlite3.Connection, *, workspace_id: str, campaign_id: str, creator_id: str, text: str) -> Enqueued:
    """Inbound creator email → one conversation per (campaign, creator)."""
    message_id = new_id()
    append_event(conn, workspace_id, "CREATOR_MESSAGE_RECEIVED", message_id, {"campaign_id": campaign_id, "creator_id": creator_id, "text": text})
    return _create_or_reuse(
        conn,
        workspace_id=workspace_id,
        thread_key=f"{campaign_id}:{creator_id}",
        trigger="CREATOR",
        campaign_id=campaign_id,
        source_ref=message_id,
        description=f"reply from creator {creator_id}",
    )


def request_cancel(conn: sqlite3.Connection, *, workspace_id: str, work_id: str) -> None:
    """Cancellation is a request; the worker applies it at its next check (between agent steps and
    before every tool call), so an in-flight side effect is never cut in half."""
    conn.execute("INSERT INTO cancellations (work_id, status, requested_at) VALUES (?, 'REQUESTED', ?)", (work_id, now()))
    append_event(conn, workspace_id, "CANCEL_REQUESTED", work_id, {})


def _create_or_reuse(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    thread_key: str,
    trigger: str,
    campaign_id: str | None,
    source_ref: str,
    description: str,
) -> Enqueued:
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            f"""SELECT work_id, active_version FROM work_items
                WHERE workspace_id = ? AND thread_key = ? AND trigger = ? AND status IN ({",".join("?" * len(_LIVE))})
                ORDER BY created_at DESC LIMIT 1""",
            (workspace_id, thread_key, trigger, *_LIVE),
        ).fetchone()
        ts = now()
        if live is not None:
            work_id, version, reused = live["work_id"], live["active_version"] + 1, True
            conn.execute("UPDATE work_items SET active_version = ? WHERE work_id = ?", (version, work_id))
            # Older PENDING runs can never be claimed now (version mismatch) — close them for a clean ledger.
            conn.execute(
                "UPDATE worker_runs SET status = 'CANCELLED', ended_at = ? WHERE work_id = ? AND status = 'PENDING' AND work_version < ?",
                (ts, work_id, version),
            )
        else:
            work_id, version, reused = new_id(), 1, False
            conn.execute(
                """INSERT INTO work_items (work_id, workspace_id, campaign_id, thread_key, trigger, status, active_version, description, created_at)
                   VALUES (?, ?, ?, ?, ?, 'CREATED', 1, ?, ?)""",
                (work_id, workspace_id, campaign_id, thread_key, trigger, description, ts),
            )
        conn.execute(
            "INSERT INTO work_versions (work_id, version, source_type, source_ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (work_id, version, trigger, source_ref, ts),
        )
        # ON CONFLICT DO NOTHING on the partial unique index: two concurrent reuses of the same version enqueue once.
        conn.execute(
            """INSERT INTO worker_runs (run_id, work_id, workspace_id, work_version, status, created_at)
               VALUES (?, ?, ?, ?, 'PENDING', ?)
               ON CONFLICT (work_id, work_version) WHERE status = 'PENDING' DO NOTHING""",
            (new_id(), work_id, workspace_id, version, ts),
        )
        append_event(conn, workspace_id, "WORK_ENQUEUED", work_id, {"version": version, "reused": reused, "trigger": trigger})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return Enqueued(work_id=work_id, version=version, reused=reused)


def approval_decision(conn: sqlite3.Connection, *, approval_id: str, approved: bool) -> Enqueued | None:
    """Owner clicked approve/reject on a card: record it and wake the work that asked (it is PAUSED)."""
    from .tools import approvals

    approvals.decide(conn, approval_id, approved)
    row = conn.execute(
        """SELECT w.workspace_id, w.thread_key, w.trigger, w.campaign_id FROM approvals a
           JOIN work_items w ON w.work_id = a.work_id WHERE a.approval_id = ?""",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    return _create_or_reuse(
        conn,
        workspace_id=row["workspace_id"],
        thread_key=row["thread_key"],
        trigger=row["trigger"],
        campaign_id=row["campaign_id"],
        source_ref=approval_id,
        description=f"approval {'granted' if approved else 'rejected'}",
    )


def add_dependency(conn: sqlite3.Connection, *, work_id: str, depends_on_work_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO work_dependencies (work_id, depends_on_work_id) VALUES (?, ?)",
        (work_id, depends_on_work_id),
    )


def describe(conn: sqlite3.Connection, work_id: str) -> dict[str, object]:
    row = conn.execute("SELECT * FROM work_items WHERE work_id = ?", (work_id,)).fetchone()
    return dict(row) if row else {}


__all__ = ["Enqueued", "add_dependency", "approval_decision", "creator_reply", "describe", "owner_message", "request_cancel"]
