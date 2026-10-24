"""ToolRunner — every tool call the agent makes goes through the same four steps.

  1. INSERT the call with its idem_key. The partial unique index makes a duplicate side effect
     impossible: a second identical call conflicts and is blocked (and the blocked attempt is still
     recorded — "nobody tried" and "tried and was blocked" must not look the same).
     FAILED/CANCELLED rows release their key first, so a call blocked by the approval gate can run
     once it is approved.
  2. Checklist, cheapest and most fundamental first:
       input contract → run still current (version / cancellation) → campaign is ACTIVE
       → approval gate.
     Activation comes before approval so the owner is never asked to approve something that cannot run.
  3. REQUESTED → RUNNING (compare-and-set), then the handler.
  4. SUCCEEDED / FAILED. A handler that fails without a reason is itself reported as a defect.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import dumps, new_id, now
from ..worker import Run, stop_reason
from . import approvals
from .registry import Effect, ToolContext, ToolSpec, campaign_of, get, idem_key
from .validation import validate


@dataclass(frozen=True)
class ToolResult:
    status: str  # SUCCEEDED | FAILED | CANCELLED
    output: dict[str, Any] | None = None
    error: str | None = None

    def for_model(self) -> str:
        """What the agent sees as the tool result."""
        if self.status == "SUCCEEDED":
            return dumps(self.output or {})
        return f"{self.status}: {self.error}"


def run_tool(conn: sqlite3.Connection, run: Run | None, workspace_id: str, campaign_id: str | None, tool_name: str, input_data: dict[str, Any]) -> ToolResult:
    try:
        spec = get(tool_name)
    except KeyError:
        return ToolResult("FAILED", error=f"unknown tool '{tool_name}'")

    call_id = new_id()
    key = idem_key(spec, input_data, campaign_id, workspace_id)

    # ── Step 1 ──
    if not _insert(conn, call_id, workspace_id, run, spec, key, input_data):
        return ToolResult(
            "CANCELLED",
            error=(
                "already proceeded — an identical action already succeeded or is in progress, so this duplicate was "
                "blocked. That record is the only thing this message asserts: if you have fresher evidence the effect "
                "is absent, do not tell the owner it was done; report the contradiction."
            ),
        )

    # ── Step 2 ──
    error = _checklist(conn, run, spec, input_data, campaign_id, key)
    if error is not None:
        _set(conn, call_id, "CANCELLED", "REQUESTED", error=error)
        return ToolResult("CANCELLED", error=error)

    # ── Step 3 ──
    if not _set(conn, call_id, "RUNNING", "REQUESTED"):
        return ToolResult("CANCELLED", error="already proceeded")
    ctx = ToolContext(conn, workspace_id, run.work_id if run else None, run.run_id if run else None, campaign_of(input_data, campaign_id))
    try:
        output = spec.handler(input_data, ctx)
    except Exception as exc:  # noqa: BLE001 — any handler crash becomes a FAILED record with its reason
        err = f"{type(exc).__name__}: {exc}"
        _set(conn, call_id, "FAILED", "RUNNING", error=err)
        return ToolResult("FAILED", error=err)

    # ── Step 4 ──
    if output.get("status") in ("FAILED", "CANCELLED"):
        err = output.get("error_log") or (
            f"handler '{tool_name}' returned {output['status']} without an error_log (keys: {sorted(output)}) — "
            "a handler defect: a failure must carry its reason"
        )
        _set(conn, call_id, str(output["status"]), "RUNNING", error=str(err))
        return ToolResult(str(output["status"]), error=str(err))
    _set(conn, call_id, "SUCCEEDED", "RUNNING", output=output)
    return ToolResult("SUCCEEDED", output=output)


def _checklist(conn: sqlite3.Connection, run: Run | None, spec: ToolSpec, input_data: dict[str, Any], ctx_campaign: str | None, key: str | None) -> str | None:
    error = validate(spec.parameters, input_data)
    if error:
        return error
    if run is not None:
        reason = stop_reason(conn, run)
        if reason:
            return f"STOPPED: this work was {reason}; do not continue it."
    campaign_id = campaign_of(input_data, ctx_campaign)
    if spec.needs_active_campaign:
        row = conn.execute("SELECT status FROM campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone() if campaign_id else None
        if row is None:
            return "NO_CAMPAIGN: pass campaign_id — this tool acts on one specific campaign."
        if row["status"] != "ACTIVE":
            return f"CAMPAIGN_NOT_ACTIVE: campaign is {row['status']}. Planning saves are fine; execution starts after campaign_starter."
    if spec.effect is not Effect.READ and approvals.needs_approval(conn, spec.name, campaign_id):
        return approvals.gate(conn, spec.name, key)
    return None


def _insert(conn: sqlite3.Connection, call_id: str, workspace_id: str, run: Run | None, spec: ToolSpec, key: str | None, input_data: dict[str, Any]) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if key is not None:
            conn.execute("UPDATE tool_calls SET idem_key = NULL WHERE idem_key = ? AND status IN ('FAILED', 'CANCELLED')", (key,))
        cur = conn.execute(
            """INSERT INTO tool_calls (tool_call_id, workspace_id, work_id, run_id, tool_name, idem_key, status, input_data, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?)
               ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING""",
            (call_id, workspace_id, run.work_id if run else None, run.run_id if run else None, spec.name, key, dumps(input_data), now()),
        )
        inserted = cur.rowcount == 1
        if not inserted:
            conn.execute(
                """INSERT INTO tool_calls (tool_call_id, workspace_id, work_id, run_id, tool_name, idem_key, status, input_data, error_log, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 'CANCELLED', ?, ?, ?)""",
                (call_id, workspace_id, run.work_id if run else None, run.run_id if run else None, spec.name, dumps(input_data), f"blocked by idem_key conflict ({key})", now()),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return inserted


def _set(conn: sqlite3.Connection, call_id: str, status: str, expected: str, *, error: str | None = None, output: dict[str, Any] | None = None) -> bool:
    cur = conn.execute(
        "UPDATE tool_calls SET status = ?, error_log = COALESCE(?, error_log), output_data = COALESCE(?, output_data) WHERE tool_call_id = ? AND status = ?",
        (status, error, json.dumps(output, ensure_ascii=False, default=str) if output is not None else None, call_id, expected),
    )
    return cur.rowcount == 1
