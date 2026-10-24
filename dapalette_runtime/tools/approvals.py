"""Owner approval gate.

Product goal: the brand should have to intervene as little as possible. So approval is a per-campaign
toggle list, not a blanket rule, and an unanswered card does not freeze the campaign forever:

  * EXPIRED → the agent proceeds on its own judgment …
  * … except for HOLD_ON_EXPIRY tools (money and irreversible actions), which stay blocked.
    This is a *block list*: a new tool proceeds on expiry unless someone deliberately adds it here.
  * ALWAYS_REQUIRE tools cannot be toggled off by the owner.

Cards are matched by idem_key — the same identity the runner computes — so an approval authorises
exactly one action, never "any call of this tool".
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import append_event, dumps, new_id, now

DEFAULT_APPROVAL_TOOLS = ("campaign_starter", "outreach_mail_sender", "contract_sender", "settlement_manager")
ALWAYS_REQUIRE = frozenset({"contract_sender", "settlement_manager"})
HOLD_ON_EXPIRY = frozenset({"contract_sender", "settlement_manager"})
APPROVAL_TTL_SECONDS = 2 * 24 * 3600


def needs_approval(conn: sqlite3.Connection, tool_name: str, campaign_id: str | None) -> bool:
    if tool_name in ALWAYS_REQUIRE:
        return True
    if campaign_id is None:
        return tool_name in DEFAULT_APPROVAL_TOOLS
    row = conn.execute("SELECT approval_needed_tools FROM campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone()
    tools: list[str] = json.loads(row["approval_needed_tools"]) if row else list(DEFAULT_APPROVAL_TOOLS)
    return tool_name in tools


def request(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    work_id: str | None,
    tool_name: str,
    idem_key: str,
    payload: dict[str, Any],
    ttl: float = APPROVAL_TTL_SECONDS,
) -> str:
    existing = conn.execute(
        "SELECT approval_id FROM approvals WHERE idem_key = ? AND status = 'REQUESTED'", (idem_key,)
    ).fetchone()
    if existing:
        return str(existing["approval_id"])
    approval_id = new_id()
    conn.execute(
        """INSERT INTO approvals (approval_id, workspace_id, work_id, tool_name, idem_key, status, payload, expires_at)
           VALUES (?, ?, ?, ?, ?, 'REQUESTED', ?, ?)""",
        (approval_id, workspace_id, work_id, tool_name, idem_key, dumps(payload), now() + ttl),
    )
    append_event(conn, workspace_id, "APPROVAL_REQUESTED", approval_id, {"tool": tool_name})
    return approval_id


def decide(conn: sqlite3.Connection, approval_id: str, approved: bool) -> None:
    conn.execute(
        "UPDATE approvals SET status = ?, decided_at = ? WHERE approval_id = ? AND status = 'REQUESTED'",
        ("APPROVED" if approved else "REJECTED", now(), approval_id),
    )


def expire_due(conn: sqlite3.Connection) -> int:
    """Run by the scheduler."""
    return conn.execute(
        "UPDATE approvals SET status = 'EXPIRED', decided_at = ? WHERE status = 'REQUESTED' AND expires_at < ?",
        (now(), now()),
    ).rowcount


def gate(conn: sqlite3.Connection, tool_name: str, idem_key: str | None) -> str | None:
    """None = may proceed. Otherwise the message the agent gets back — written to steer its next step."""
    if idem_key is None:
        return None
    row = conn.execute(
        "SELECT status FROM approvals WHERE idem_key = ? ORDER BY expires_at DESC LIMIT 1", (idem_key,)
    ).fetchone()
    status = row["status"] if row else None
    if status == "APPROVED":
        return None
    if status == "EXPIRED":
        if tool_name in HOLD_ON_EXPIRY:
            return (
                f"APPROVAL_EXPIRED: '{tool_name}' moves money or cannot be undone, so it stays blocked after the "
                "owner did not answer. Do not create the same card again; tell the owner it is waiting on them."
            )
        return None  # proceed: decide within the campaign's goals and brand constraints, don't re-ask
    if status == "REJECTED":
        return (
            f"APPROVAL_REJECTED: the owner rejected this exact '{tool_name}' action. Do not re-request it unchanged; "
            "ask what they want instead or propose a different action."
        )
    if status == "REQUESTED":
        return f"APPROVAL_PENDING: '{tool_name}' is waiting for the owner. Continue other work; this call will pass once approved."
    return (
        f"APPROVAL_REQUIRED: '{tool_name}' needs owner approval for this exact action. "
        f"Call request_approval with tool_name='{tool_name}' and the same arguments (idem_key={idem_key})."
    )
