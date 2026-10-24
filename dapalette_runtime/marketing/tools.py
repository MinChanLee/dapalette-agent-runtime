"""Influencer-marketing tools, condensed.

The campaign journey the agent runs end to end:

    plan (PLANNING) → start (ACTIVE) → shortlist creators → outreach → contract
    → ship product → review the post → settle payment

Division of labour, the same everywhere in this codebase: **the agent judges, the code enforces.**
Strategy (which creators fit, how to phrase outreach) is the agent's. Amounts, stage order, budget limits
and disclosure rules are checked here, in code, and the current numbers are returned so the agent never
has to guess or recompute them.

Production has ~40 tools with negotiation, contracts, multi-country shipping, rights and analytics;
these handlers keep each tool's contract and invariants and replace external providers with an outbox.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import append_event, dumps, new_id, now
from ..tools import approvals
from ..tools.registry import Effect, ToolContext, ToolSpec, get, idem_key, register

STAGES = ("CONTACTED", "CONTRACTED", "SHIPPED", "POSTED", "SETTLED")

# Korean FTC endorsement guidelines require paid content to be disclosed, visibly, in the post itself.
_DISCLOSURE = re.compile(r"#광고|#유료광고|유료\s*광고|#ad\b|#sponsored\b|paid partnership", re.IGNORECASE)

CAMPAIGN_ID = {"type": "string", "description": "the campaign this action belongs to"}
CREATOR_ID = {"type": "string"}


def _fail(msg: str) -> dict[str, Any]:
    return {"status": "FAILED", "error_log": msg}


def _outbox(ctx: ToolContext, channel: str, recipient: str, body: str) -> None:
    ctx.conn.execute(
        "INSERT INTO outbox (channel, recipient, body, created_at) VALUES (?, ?, ?, ?)", (channel, recipient, body, now())
    )


def _campaign(ctx: ToolContext) -> dict[str, Any]:
    row = ctx.conn.execute("SELECT * FROM campaigns WHERE campaign_id = ?", (ctx.campaign_id,)).fetchone()
    if row is None:
        raise LookupError(f"campaign {ctx.campaign_id} not found")
    return {**dict(row), "brief": json.loads(row["brief"])}


def _collab(ctx: ToolContext, creator_id: str) -> dict[str, Any] | None:
    row = ctx.conn.execute(
        "SELECT * FROM collaborations WHERE campaign_id = ? AND creator_id = ?", (ctx.campaign_id, creator_id)
    ).fetchone()
    return dict(row) if row else None


def _require_stage(ctx: ToolContext, creator_id: str, expected: str) -> dict[str, Any] | str:
    c = _collab(ctx, creator_id)
    if c is None:
        return f"creator {creator_id} has not been contacted for this campaign"
    if c["stage"] != expected:
        return f"creator {creator_id} is at stage {c['stage']}; this step needs {expected}"
    return c


def _budget(ctx: ToolContext) -> dict[str, int]:
    total = int(_campaign(ctx)["brief"]["budget_krw"])
    committed = ctx.conn.execute(
        "SELECT COALESCE(SUM(fee_krw), 0) AS s FROM collaborations WHERE campaign_id = ?", (ctx.campaign_id,)
    ).fetchone()["s"]
    return {"budget_krw": total, "committed_krw": int(committed), "remaining_krw": total - int(committed)}


# ── planning ────────────────────────────────────────────────────────────────


def _campaign_maker(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    campaign_id = new_id()
    brief = {k: inp[k] for k in ("product", "kpi_posts", "budget_krw", "end_date")}
    ctx.conn.execute(
        "INSERT INTO campaigns (campaign_id, workspace_id, name, status, brief, approval_needed_tools, created_at) VALUES (?, ?, ?, 'PLANNING', ?, ?, ?)",
        (campaign_id, ctx.workspace_id, inp["name"], dumps(brief), dumps(list(approvals.DEFAULT_APPROVAL_TOOLS)), now()),
    )
    if ctx.work_id:
        ctx.conn.execute("UPDATE work_items SET campaign_id = ? WHERE work_id = ? AND campaign_id IS NULL", (campaign_id, ctx.work_id))
    return {"campaign_id": campaign_id, "status": "PLANNING", "brief": brief}


def _campaign_starter(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    c = _campaign(ctx)
    if c["status"] != "PLANNING":
        return _fail(f"campaign is already {c['status']}")
    ctx.conn.execute("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = ?", (ctx.campaign_id,))
    append_event(ctx.conn, ctx.workspace_id, "CAMPAIGN_STARTED", ctx.campaign_id, {})
    return {"campaign_id": ctx.campaign_id, "status": "ACTIVE"}


def _campaign_status(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The facts the agent plans from — computed by code, not re-derived by the model."""
    c = _campaign(ctx)
    stages = {s: 0 for s in STAGES}
    for row in ctx.conn.execute("SELECT stage, COUNT(*) AS n FROM collaborations WHERE campaign_id = ? GROUP BY stage", (ctx.campaign_id,)):
        stages[row["stage"]] = row["n"]
    posted = stages["POSTED"] + stages["SETTLED"]
    return {
        "status": c["status"],
        "kpi_posts": c["brief"]["kpi_posts"],
        "posts_so_far": posted,
        "posts_missing": max(0, int(c["brief"]["kpi_posts"]) - posted),
        "stages": stages,
        **_budget(ctx),
        "pending_approvals": [
            dict(r) for r in ctx.conn.execute("SELECT approval_id, tool_name, payload FROM approvals WHERE status = 'REQUESTED' AND workspace_id = ?", (ctx.workspace_id,))
        ],
    }


# ── sourcing & outreach ─────────────────────────────────────────────────────


def _creator_shortlister(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    rows = ctx.conn.execute(
        """SELECT c.creator_id, c.handle, c.platform, c.followers, c.category FROM creators c
           WHERE c.category = ? AND c.followers >= ?
             AND NOT EXISTS (SELECT 1 FROM collaborations k WHERE k.campaign_id = ? AND k.creator_id = c.creator_id)
           ORDER BY c.followers DESC LIMIT ?""",
        (inp["category"], inp.get("min_followers", 0), ctx.campaign_id, inp.get("limit", 10)),
    ).fetchall()
    return {"creators": [dict(r) for r in rows]}


def _outreach_mail_sender(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sent, skipped = [], []
    for creator_id in inp["creator_ids"]:
        if _collab(ctx, creator_id) is not None:
            skipped.append({"creator_id": creator_id, "reason": "already contacted for this campaign"})
            continue
        creator = ctx.conn.execute("SELECT email FROM creators WHERE creator_id = ?", (creator_id,)).fetchone()
        if creator is None:
            skipped.append({"creator_id": creator_id, "reason": "unknown creator"})
            continue
        _outbox(ctx, "email", creator["email"], inp["message"])
        ctx.conn.execute("INSERT INTO collaborations (campaign_id, creator_id, stage) VALUES (?, ?, 'CONTACTED')", (ctx.campaign_id, creator_id))
        sent.append(creator_id)
    return {"sent": sent, "skipped": skipped}


# ── contract → shipping → content → settlement ──────────────────────────────


def _contract_sender(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    c = _require_stage(ctx, inp["creator_id"], "CONTACTED")
    if isinstance(c, str):
        return _fail(c)
    budget = _budget(ctx)
    if inp["fee_krw"] > budget["remaining_krw"]:
        return _fail(f"fee {inp['fee_krw']} exceeds the remaining budget {budget['remaining_krw']} (budget {budget['budget_krw']}, committed {budget['committed_krw']})")
    ctx.conn.execute(
        "UPDATE collaborations SET stage = 'CONTRACTED', fee_krw = ? WHERE campaign_id = ? AND creator_id = ?",
        (inp["fee_krw"], ctx.campaign_id, inp["creator_id"]),
    )
    email = ctx.conn.execute("SELECT email FROM creators WHERE creator_id = ?", (inp["creator_id"],)).fetchone()["email"]
    _outbox(ctx, "esign", email, f"contract for {inp['fee_krw']} KRW")
    return {"creator_id": inp["creator_id"], "stage": "CONTRACTED", **_budget(ctx)}


def _shipment_recorder(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    c = _require_stage(ctx, inp["creator_id"], "CONTRACTED")
    if isinstance(c, str):
        return _fail(c)
    ctx.conn.execute(
        "UPDATE collaborations SET stage = 'SHIPPED', tracking_no = ? WHERE campaign_id = ? AND creator_id = ?",
        (inp["tracking_no"], ctx.campaign_id, inp["creator_id"]),
    )
    return {"creator_id": inp["creator_id"], "stage": "SHIPPED"}


def _content_reviewer(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Format and legal rules are checked by code; whether the content fits the brief is the agent's call."""
    c = _require_stage(ctx, inp["creator_id"], "SHIPPED")
    if isinstance(c, str):
        return _fail(c)
    if not _DISCLOSURE.search(inp["caption"]):
        return {
            "creator_id": inp["creator_id"],
            "accepted": False,
            "reason": "no paid-partnership disclosure (e.g. #광고) in the caption — ask the creator to add it; the post does not count yet",
        }
    ctx.conn.execute(
        "UPDATE collaborations SET stage = 'POSTED', post_url = ? WHERE campaign_id = ? AND creator_id = ?",
        (inp["post_url"], ctx.campaign_id, inp["creator_id"]),
    )
    return {"creator_id": inp["creator_id"], "accepted": True, "stage": "POSTED"}


def _settlement_manager(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    c = _require_stage(ctx, inp["creator_id"], "POSTED")
    if isinstance(c, str):
        return _fail(c)
    owed = int(c["fee_krw"]) - int(c["paid_krw"])
    if inp["amount_krw"] > owed:
        return _fail(f"amount {inp['amount_krw']} exceeds what is owed under the contract ({owed})")
    paid = int(c["paid_krw"]) + inp["amount_krw"]
    stage = "SETTLED" if paid == int(c["fee_krw"]) else "POSTED"
    ctx.conn.execute(
        "UPDATE collaborations SET paid_krw = ?, stage = ? WHERE campaign_id = ? AND creator_id = ?",
        (paid, stage, ctx.campaign_id, inp["creator_id"]),
    )
    _outbox(ctx, "payment", inp["creator_id"], f"{inp['amount_krw']} KRW")
    return {"creator_id": inp["creator_id"], "paid_krw": paid, "owed_krw": int(c["fee_krw"]) - paid, "stage": stage}


# ── talking to the owner ────────────────────────────────────────────────────


def _request_approval(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = get(inp["tool_name"])
    args = inp["arguments"]
    key = idem_key(target, args, ctx.campaign_id, ctx.workspace_id)  # the same function the runner gates with
    if key is None:
        return _fail(f"'{target.name}' has no side effects and needs no approval")
    approval_id = approvals.request(
        ctx.conn,
        workspace_id=ctx.workspace_id,
        work_id=ctx.work_id,
        tool_name=target.name,
        idem_key=key,
        payload={"summary": inp["summary"], "arguments": args},
    )
    return {"approval_id": approval_id, "status": "REQUESTED", "on_expiry": "blocked" if target.name in approvals.HOLD_ON_EXPIRY else "proceeds"}


def _owner_reply(inp: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _outbox(ctx, "owner_chat", ctx.workspace_id, inp["message"])
    return {"posted": True}


def _p(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def register_all() -> None:
    register(ToolSpec(
        "campaign_maker", "Create a campaign in PLANNING with its brief.",
        _p({"name": {"type": "string"}, "product": {"type": "string"}, "kpi_posts": {"type": "integer", "minimum": 1},
            "budget_krw": {"type": "integer", "minimum": 0}, "end_date": {"type": "string"}},
           ["name", "product", "kpi_posts", "budget_krw", "end_date"]),
        Effect.WRITE, _campaign_maker, idem_fields=("name", "product"),
    ))
    register(ToolSpec(
        "campaign_starter", "Move a planned campaign to ACTIVE so execution tools can run.",
        _p({"campaign_id": CAMPAIGN_ID}, ["campaign_id"]), Effect.WRITE, _campaign_starter, idem_fields=("campaign_id",),
    ))
    register(ToolSpec(
        "campaign_status", "KPI progress, pipeline stages, budget and pending approvals for a campaign.",
        _p({"campaign_id": CAMPAIGN_ID}, ["campaign_id"]), Effect.READ, _campaign_status,
    ))
    register(ToolSpec(
        "creator_shortlister", "Find creators not yet contacted for this campaign.",
        _p({"campaign_id": CAMPAIGN_ID, "category": {"type": "string"}, "min_followers": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1}}, ["campaign_id", "category"]),
        Effect.READ, _creator_shortlister,
    ))
    register(ToolSpec(
        "outreach_mail_sender", "Email a collaboration proposal to creators. Already-contacted creators are skipped.",
        _p({"campaign_id": CAMPAIGN_ID, "creator_ids": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}},
           ["campaign_id", "creator_ids", "message"]),
        Effect.EXTERNAL, _outreach_mail_sender, idem_fields=("creator_ids",), needs_active_campaign=True,
    ))
    register(ToolSpec(
        "contract_sender", "Send a contract for signature at an agreed fee.",
        _p({"campaign_id": CAMPAIGN_ID, "creator_id": CREATOR_ID, "fee_krw": {"type": "integer", "minimum": 0}},
           ["campaign_id", "creator_id", "fee_krw"]),
        Effect.EXTERNAL, _contract_sender, idem_fields=("creator_id", "fee_krw"), needs_active_campaign=True,
    ))
    register(ToolSpec(
        "shipment_recorder", "Record that the product was shipped to a contracted creator.",
        _p({"campaign_id": CAMPAIGN_ID, "creator_id": CREATOR_ID, "tracking_no": {"type": "string"}},
           ["campaign_id", "creator_id", "tracking_no"]),
        Effect.WRITE, _shipment_recorder, idem_fields=("creator_id",), needs_active_campaign=True,
    ))
    register(ToolSpec(
        "content_reviewer", "Review a creator's published post against the campaign rules.",
        _p({"campaign_id": CAMPAIGN_ID, "creator_id": CREATOR_ID, "post_url": {"type": "string"}, "caption": {"type": "string"}},
           ["campaign_id", "creator_id", "post_url", "caption"]),
        Effect.WRITE, _content_reviewer, idem_fields=("creator_id", "post_url", "caption"), needs_active_campaign=True,
    ))
    register(ToolSpec(
        "settlement_manager", "Pay a creator for an accepted post, up to what the contract still owes.",
        _p({"campaign_id": CAMPAIGN_ID, "creator_id": CREATOR_ID, "amount_krw": {"type": "integer", "minimum": 1}},
           ["campaign_id", "creator_id", "amount_krw"]),
        Effect.MONEY, _settlement_manager, idem_fields=("creator_id", "amount_krw"), needs_active_campaign=True,
    ))
    register(ToolSpec(
        "request_approval", "Ask the owner to approve one exact action (same tool and arguments you will call).",
        _p({"tool_name": {"type": "string"}, "arguments": {"type": "object"}, "summary": {"type": "string"}},
           ["tool_name", "arguments", "summary"]),
        Effect.WRITE, _request_approval, dedupe=False,
    ))
    register(ToolSpec(
        "owner_reply", "Post a message to the owner's thread.",
        _p({"message": {"type": "string"}}, ["message"]), Effect.WRITE, _owner_reply, dedupe=False,
    ))
