from __future__ import annotations

from typing import Any

from dapalette_runtime.tools import approvals, registry
from dapalette_runtime.tools.registry import Effect, ToolSpec
from dapalette_runtime.tools.runner import run_tool

from .helpers import WS, approve_all, claimed_run, mem_db, owner_work, rows, seed_campaign


def call(conn: Any, tool: str, args: dict[str, Any], run: Any = None, campaign_id: str | None = None):
    return run_tool(conn, run, WS, campaign_id, tool, args)


def test_identical_action_runs_once_and_the_blocked_attempt_is_recorded() -> None:
    conn = mem_db()
    cid = seed_campaign(conn, approval_tools=[])
    args = {"campaign_id": cid, "creator_ids": ["cr0", "cr1"], "message": "hi"}
    assert call(conn, "outreach_mail_sender", args).status == "SUCCEEDED"
    # same recipients in a different order, different wording → still the same action
    again = call(conn, "outreach_mail_sender", {**args, "creator_ids": ["cr1", "cr0"], "message": "hello!"})
    assert again.status == "CANCELLED" and "already proceeded" in (again.error or "")
    assert len(rows(conn, "SELECT * FROM outbox WHERE channel = 'email'")) == 2
    assert rows(conn, "SELECT status, idem_key IS NULL AS no_key FROM tool_calls ORDER BY created_at") == [
        {"status": "SUCCEEDED", "no_key": 0},
        {"status": "CANCELLED", "no_key": 1},
    ]


def test_failed_call_releases_its_key_so_a_retry_can_run() -> None:
    conn = mem_db()
    cid = seed_campaign(conn, approval_tools=[])
    args = {"campaign_id": cid, "creator_id": "cr0", "tracking_no": "X1"}
    assert call(conn, "shipment_recorder", args).status == "FAILED"  # not contracted yet
    conn.execute("INSERT INTO collaborations (campaign_id, creator_id, stage, fee_krw) VALUES (?, 'cr0', 'CONTRACTED', 100)", (cid,))
    assert call(conn, "shipment_recorder", args).status == "SUCCEEDED"


def test_invalid_input_returns_the_declared_contract() -> None:
    conn = mem_db()
    cid = seed_campaign(conn)
    r = call(conn, "contract_sender", {"campaign_id": cid, "creator_id": "cr0", "fee_krw": "1500000", "note": "x"})
    assert r.status == "CANCELLED"
    assert "'fee_krw' must be integer" in (r.error or "") and "unknown argument 'note'" in (r.error or "")
    assert "Declared arguments: campaign_id: string (required)" in (r.error or "")


def test_inactive_campaign_is_refused_before_asking_the_owner_anything() -> None:
    conn = mem_db()
    cid = seed_campaign(conn, status="PLANNING")
    r = call(conn, "outreach_mail_sender", {"campaign_id": cid, "creator_ids": ["cr0"], "message": "hi"})
    assert (r.error or "").startswith("CAMPAIGN_NOT_ACTIVE")
    assert rows(conn, "SELECT * FROM approvals") == []


def test_approval_flow_matches_the_exact_action() -> None:
    conn = mem_db()
    run = claimed_run(conn)
    cid = seed_campaign(conn)
    args = {"campaign_id": cid, "creator_ids": ["cr0"], "message": "hi"}
    assert (call(conn, "outreach_mail_sender", args, run).error or "").startswith("APPROVAL_REQUIRED")
    assert call(conn, "request_approval", {"tool_name": "outreach_mail_sender", "arguments": args, "summary": "s"}, run).status == "SUCCEEDED"
    assert (call(conn, "outreach_mail_sender", args, run).error or "").startswith("APPROVAL_PENDING")
    approve_all(conn)
    # approval covers this action only — a different recipient set is a different action
    assert (call(conn, "outreach_mail_sender", {**args, "creator_ids": ["cr1"]}, run).error or "").startswith("APPROVAL_REQUIRED")
    assert call(conn, "outreach_mail_sender", args, run).status == "SUCCEEDED"


def test_rejected_card_is_not_silently_retried() -> None:
    conn = mem_db()
    cid = seed_campaign(conn)
    args = {"campaign_id": cid, "creator_ids": ["cr0"], "message": "hi"}
    call(conn, "request_approval", {"tool_name": "outreach_mail_sender", "arguments": args, "summary": "s"})
    approvals.decide(conn, rows(conn, "SELECT approval_id FROM approvals")[0]["approval_id"], False)
    assert (call(conn, "outreach_mail_sender", args).error or "").startswith("APPROVAL_REJECTED")


def test_expiry_proceeds_for_ordinary_tools_but_holds_money() -> None:
    conn = mem_db()
    cid = seed_campaign(conn)
    conn.execute("INSERT INTO collaborations (campaign_id, creator_id, stage, fee_krw) VALUES (?, 'cr0', 'POSTED', 100)", (cid,))
    outreach = {"campaign_id": cid, "creator_ids": ["cr1"], "message": "hi"}
    pay = {"campaign_id": cid, "creator_id": "cr0", "amount_krw": 100}
    for tool, args in (("outreach_mail_sender", outreach), ("settlement_manager", pay)):
        call(conn, "request_approval", {"tool_name": tool, "arguments": args, "summary": "s"})
    conn.execute("UPDATE approvals SET expires_at = 0")
    assert approvals.expire_due(conn) == 2
    assert call(conn, "outreach_mail_sender", outreach).status == "SUCCEEDED"
    assert (call(conn, "settlement_manager", pay).error or "").startswith("APPROVAL_EXPIRED")


def test_owner_cannot_toggle_off_contract_approval() -> None:
    conn = mem_db()
    cid = seed_campaign(conn, approval_tools=[])
    conn.execute("INSERT INTO collaborations (campaign_id, creator_id, stage) VALUES (?, 'cr0', 'CONTACTED')", (cid,))
    r = call(conn, "contract_sender", {"campaign_id": cid, "creator_id": "cr0", "fee_krw": 100})
    assert (r.error or "").startswith("APPROVAL_REQUIRED")


def test_tool_calls_stop_once_the_work_is_superseded() -> None:
    conn = mem_db()
    run = claimed_run(conn)
    owner_work(conn, text="@AI new instruction")
    r = call(conn, "owner_reply", {"message": "done"}, run)
    assert (r.error or "").startswith("STOPPED: this work was superseded")
    assert rows(conn, "SELECT * FROM outbox") == []


def test_handler_failure_without_reason_is_reported_as_a_defect() -> None:
    registry.register(ToolSpec("broken_tool", "x", {"type": "object", "properties": {}}, Effect.WRITE, lambda i, c: {"status": "FAILED"}, dedupe=False))
    r = call(mem_db(), "broken_tool", {})
    assert r.status == "FAILED" and "a failure must carry its reason" in (r.error or "")


def test_idem_key_is_stable_when_a_creating_tool_changes_its_own_context() -> None:
    """Regression: campaign_maker's key used to include the work's campaign — which campaign_maker itself
    sets — so a replay after success computed a new key and created a second campaign."""
    spec = registry.get("campaign_maker")
    args = {"name": "n", "product": "p", "kpi_posts": 1, "budget_krw": 1, "end_date": "d"}
    assert registry.idem_key(spec, args, None, WS) == registry.idem_key(spec, args, "campaign-created-by-it", WS)
    assert registry.idem_key(spec, args, None, WS) != registry.idem_key(spec, args, None, "another-workspace")


def test_every_gated_tool_is_registered_and_deduplicated() -> None:
    gated = set(approvals.DEFAULT_APPROVAL_TOOLS) | approvals.ALWAYS_REQUIRE | approvals.HOLD_ON_EXPIRY
    for name in gated:
        spec = registry.get(name)  # a typo here would silently disable a gate
        assert spec.effect is not Effect.READ and spec.dedupe, name


def test_schemas_and_idempotency_fields_agree() -> None:
    for spec in registry.all_tools():
        props = spec.parameters.get("properties", {})
        assert set(spec.parameters.get("required", [])) <= set(props), spec.name
        assert set(spec.idem_fields) <= set(props), spec.name

