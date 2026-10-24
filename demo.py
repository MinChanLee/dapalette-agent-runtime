"""End-to-end walkthrough of one seeding campaign on the runtime.

The "model" here is a scripted stand-in (no API key needed): each run it walks the same intended
plan from the top. That is a useful stress test by itself — every re-run replays actions that already
happened, and the runtime, not the script, is what keeps them from happening twice.

    python demo.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import dapalette_runtime  # noqa: F401  (registers the marketing tools)
from dapalette_runtime import control_plane as cp
from dapalette_runtime.agent import LLM, ToolCall, Transcript, work_once
from dapalette_runtime.db import connect
from dapalette_runtime.tools import approvals
from dapalette_runtime.worker import Run

WS = "ws-sunlab"
WORKER = "worker-1"
Step = Callable[[dict[str, Any]], ToolCall | None]


class ScriptedModel:
    """Walks a plan; on an approval refusal it asks for that exact action and ends the turn."""

    def __init__(self, plan: list[Step], state: dict[str, Any]) -> None:
        self.plan, self.state, self.i = plan, state, 0
        self.asked = False

    def next_calls(self, transcript: Transcript, tools: list[dict[str, Any]]) -> list[ToolCall]:
        last = transcript.messages[-1]
        if last["role"] == "tool":
            result: str = last["content"]["result"]
            print(f"      ← {last['content']['tool']}: {result[:150]}")
            if last["content"]["tool"] == "campaign_maker" and result.startswith("{"):
                self.state["campaign_id"] = json.loads(result)["campaign_id"]
            if result.startswith(("CANCELLED: APPROVAL_REQUIRED", "CANCELLED: APPROVAL_PENDING")) and not self.asked:
                self.asked = True
                prev = transcript.messages[-2]["content"][0]
                if result.startswith("CANCELLED: APPROVAL_PENDING"):
                    return []
                print(f"      → request_approval for {prev['tool']}")
                return [ToolCall("request_approval", {"tool_name": prev["tool"], "arguments": prev["arguments"], "summary": f"{prev['tool']} {prev['arguments']}"})]
            if self.asked:
                return []
        while self.i < len(self.plan):
            call = self.plan[self.i](self.state)
            self.i += 1
            if call is not None:
                print(f"      → {call.name} {json.dumps(call.arguments, ensure_ascii=False)[:110]}")
                return [call]
        return []


def section(title: str) -> None:
    print(f"\n━━ {title}")


def run_worker(conn: Any, plan: list[Step], state: dict[str, Any]) -> None:
    def model_for(run: Run) -> LLM:
        print(f"   [worker] claimed run v{run.work_version} of work {run.work_id[:8]}")
        return ScriptedModel(plan, state)

    outcome = work_once(conn, lambda: connect(DB), WORKER, model_for)
    if outcome is None:
        print("   [worker] nothing to claim")
    else:
        print(f"   [worker] run {outcome.status}{' — paused, waiting for the owner' if outcome.waiting_for_approval else ''}")


def approve_pending(conn: Any, approve: bool = True) -> None:
    for row in conn.execute("SELECT approval_id, tool_name FROM approvals WHERE status = 'REQUESTED'").fetchall():
        print(f"   [owner] {'approves' if approve else 'rejects'} {row['tool_name']}")
        cp.approval_decision(conn, approval_id=row["approval_id"], approved=approve)


DB = "file:demo?mode=memory&cache=shared"


def main() -> None:
    conn = connect(DB)
    for i, (handle, followers, category) in enumerate(
        [("demo_beauty_a", 182_000, "beauty"), ("demo_beauty_b", 96_000, "beauty"), ("demo_beauty_c", 54_000, "beauty"), ("demo_tech_a", 300_000, "tech")]
    ):
        conn.execute("INSERT INTO creators VALUES (?, ?, 'instagram', ?, ?, ?)", (f"cr{i}", handle, followers, category, f"{handle}@example.com"))

    state: dict[str, Any] = {}
    cid: Callable[[dict[str, Any]], str] = lambda s: s["campaign_id"]  # noqa: E731

    launch: list[Step] = [
        lambda s: ToolCall("campaign_maker", {"name": "Sun Lab SPF50 seeding", "product": "SPF50 sun cream", "kpi_posts": 2, "budget_krw": 3_000_000, "end_date": "2026-12-15"}),
        lambda s: ToolCall("campaign_starter", {"campaign_id": cid(s)}),
        lambda s: ToolCall("creator_shortlister", {"campaign_id": cid(s), "category": "beauty", "min_followers": 50_000, "limit": 2}),
        lambda s: ToolCall("outreach_mail_sender", {"campaign_id": cid(s), "creator_ids": ["cr0", "cr1"], "message": "안녕하세요, 선랩 SPF50 협업 제안드립니다."}),
        lambda s: ToolCall("owner_reply", {"message": "두 분께 제안 메일을 보냈어요. 회신 오면 계약 진행할게요."}),
    ]

    section("1. Owner asks in chat. Without @AI it is just a message between people; with it, work is created.")
    print("   ", cp.owner_message(conn, workspace_id=WS, thread_key="t-1", text="선크림 시딩 해보자"))
    enq = cp.owner_message(conn, workspace_id=WS, thread_key="t-1", text="@AI 선크림 시딩 캠페인 만들어서 돌려줘. 크리에이터 2명, 예산 300만원")
    print("   ", enq)

    section("2. Run 1 — plans the campaign; starting it needs the owner's OK, so the agent asks and pauses.")
    run_worker(conn, launch, state)

    section("3. Owner approves → the work wakes up. Run 2 replays the plan from the top:")
    print("   campaign_maker is not executed twice — the runtime blocks the duplicate, not the script.")
    approve_pending(conn)
    run_worker(conn, launch, state)

    section("4. Outreach leaves the system, so it needs approval too. Approve → run 3 sends it.")
    approve_pending(conn)
    run_worker(conn, launch, state)
    print("   outbox:", [r["recipient"] for r in conn.execute("SELECT recipient FROM outbox WHERE channel = 'email'")])

    section("5. A creator replies. Contracts always need approval — and approval never overrides the budget.")
    deal: list[Step] = [
        lambda s: ToolCall("contract_sender", {"campaign_id": cid(s), "creator_id": "cr0", "fee_krw": 3_500_000}),
        lambda s: ToolCall("contract_sender", {"campaign_id": cid(s), "creator_id": "cr0", "fee_krw": 1_500_000}),
    ]
    cp.creator_reply(conn, workspace_id=WS, campaign_id=state["campaign_id"], creator_id="cr0", text="좋아요! 350만원 가능할까요? 어렵다면 150만원도 괜찮아요.")
    run_worker(conn, deal, state)
    approve_pending(conn)
    run_worker(conn, deal, state)
    approve_pending(conn)
    run_worker(conn, deal, state)

    section("6. Ship → a post without #광고 is not accepted (legal rule, in code) → the fixed post is.")
    post: list[Step] = [
        lambda s: ToolCall("shipment_recorder", {"campaign_id": cid(s), "creator_id": "cr0", "tracking_no": "CJ123456789"}),
        lambda s: ToolCall("content_reviewer", {"campaign_id": cid(s), "creator_id": "cr0", "post_url": "https://instagram.com/p/abc", "caption": "요즘 매일 바르는 선크림 ☀️"}),
        lambda s: ToolCall("content_reviewer", {"campaign_id": cid(s), "creator_id": "cr0", "post_url": "https://instagram.com/p/abc", "caption": "요즘 매일 바르는 선크림 ☀️ #광고"}),
        lambda s: ToolCall("campaign_status", {"campaign_id": cid(s)}),
    ]
    cp.creator_reply(conn, workspace_id=WS, campaign_id=state["campaign_id"], creator_id="cr0", text="업로드했어요!")
    run_worker(conn, post, state)

    section("7. Payment card goes unanswered and expires: other tools would proceed, money stays blocked.")
    pay: list[Step] = [lambda s: ToolCall("settlement_manager", {"campaign_id": cid(s), "creator_id": "cr0", "amount_krw": 1_500_000})]
    cp.owner_message(conn, workspace_id=WS, thread_key="t-1", text="@AI 정산해줘")
    run_worker(conn, pay, state)
    conn.execute("UPDATE approvals SET expires_at = 0 WHERE status = 'REQUESTED'")
    print(f"   [scheduler] expired {approvals.expire_due(conn)} approval card(s)")
    cp.owner_message(conn, workspace_id=WS, thread_key="t-1", text="@AI 정산 어떻게 됐어?")
    run_worker(conn, pay, state)

    section("Ledger")
    for r in conn.execute("SELECT tool_name, status, COALESCE(error_log, '') AS e FROM tool_calls ORDER BY created_at"):
        print(f"   {r['tool_name']:<22} {r['status']:<10} {r['e'][:70]}")


if __name__ == "__main__":
    main()
