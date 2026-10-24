from __future__ import annotations

from typing import Any

from dapalette_runtime.tools.runner import run_tool

from .helpers import WS, mem_db, rows, seed_campaign


def call(conn: Any, tool: str, args: dict[str, Any]):
    return run_tool(conn, None, WS, None, tool, args)


def setup(stage: str, fee: int | None = None, budget: int = 3_000_000) -> tuple[Any, str]:
    conn = mem_db()
    cid = seed_campaign(conn, approval_tools=[], budget=budget)
    conn.execute("INSERT INTO collaborations (campaign_id, creator_id, stage, fee_krw) VALUES (?, 'cr0', ?, ?)", (cid, stage, fee))
    return conn, cid


def approved(conn: Any, tool: str, args: dict[str, Any]) -> Any:
    """Contracts and payments can never be toggled off — get the exact action approved, then call it."""
    run_tool(conn, None, WS, None, "request_approval", {"tool_name": tool, "arguments": args, "summary": "s"})
    conn.execute("UPDATE approvals SET status = 'APPROVED' WHERE status = 'REQUESTED'")
    return call(conn, tool, args)


def test_contract_fee_is_limited_by_remaining_budget_even_when_approved() -> None:
    conn, cid = setup("CONTACTED", budget=1_000_000)
    r = approved(conn, "contract_sender", {"campaign_id": cid, "creator_id": "cr0", "fee_krw": 1_200_000})
    assert r.status == "FAILED" and "exceeds the remaining budget" in (r.error or "")
    ok = approved(conn, "contract_sender", {"campaign_id": cid, "creator_id": "cr0", "fee_krw": 800_000})
    assert ok.status == "SUCCEEDED" and ok.output and ok.output["remaining_krw"] == 200_000


def test_stage_order_is_enforced() -> None:
    conn, cid = setup("CONTACTED")
    r = call(conn, "content_reviewer", {"campaign_id": cid, "creator_id": "cr0", "post_url": "u", "caption": "#광고"})
    assert r.status == "FAILED" and "needs SHIPPED" in (r.error or "")


def test_post_without_disclosure_does_not_count() -> None:
    conn, cid = setup("SHIPPED", fee=100)
    base = {"campaign_id": cid, "creator_id": "cr0", "post_url": "u"}
    r = call(conn, "content_reviewer", {**base, "caption": "best sun cream ever"}).output
    assert r is not None and r["accepted"] is False and "#광고" in r["reason"]
    assert rows(conn, "SELECT stage FROM collaborations") == [{"stage": "SHIPPED"}]
    for caption in ("love it #광고", "유료 광고 포함", "my pick #ad"):
        conn.execute("UPDATE collaborations SET stage = 'SHIPPED'")
        out = call(conn, "content_reviewer", {**base, "caption": caption}).output
        assert out is not None and out["accepted"] is True, caption


def test_settlement_cannot_exceed_what_the_contract_owes() -> None:
    conn, cid = setup("POSTED", fee=1_500_000)
    over = approved(conn, "settlement_manager", {"campaign_id": cid, "creator_id": "cr0", "amount_krw": 2_000_000})
    assert over.status == "FAILED" and "exceeds what is owed" in (over.error or "")
    part = approved(conn, "settlement_manager", {"campaign_id": cid, "creator_id": "cr0", "amount_krw": 1_000_000})
    assert part.output and part.output["owed_krw"] == 500_000 and part.output["stage"] == "POSTED"
    rest = approved(conn, "settlement_manager", {"campaign_id": cid, "creator_id": "cr0", "amount_krw": 500_000})
    assert rest.output and rest.output["stage"] == "SETTLED"
    assert len(rows(conn, "SELECT * FROM outbox WHERE channel = 'payment'")) == 2


def test_campaign_status_reports_kpi_and_budget_from_the_ledger() -> None:
    conn, cid = setup("POSTED", fee=1_000_000)
    s = call(conn, "campaign_status", {"campaign_id": cid}).output
    assert s and (s["posts_so_far"], s["posts_missing"], s["committed_krw"], s["remaining_krw"]) == (1, 1, 1_000_000, 2_000_000)
