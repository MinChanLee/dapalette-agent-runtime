"""Agent loop on native tool calling, plus the worker step that drives it.

The loop is deliberately thin: the model chooses tools; everything that must hold regardless of what
the model chooses (idempotency, approvals, stage order, budget, stopping when superseded) lives in the
runner and the tools. Tool results — including refusals — go back to the model verbatim, and refusal
messages are written to steer the next step (what to do instead), not just to say no.

Production also routes across model providers, builds the system prompt from campaign/workspace
context, and records token usage per campaign; that is out of scope here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .db import dumps
from .tools import registry
from .tools.runner import ToolResult, run_tool
from .worker import Heartbeat, Run, claim, finish, stop_reason


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class Transcript:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, content: Any) -> None:
        self.messages.append({"role": role, "content": content})


class LLM(Protocol):
    def next_calls(self, transcript: Transcript, tools: list[dict[str, Any]]) -> list[ToolCall]:
        """Tool calls for the next step; an empty list ends the turn."""
        ...


@dataclass(frozen=True)
class AgentOutcome:
    status: str  # SUCCEEDED | FAILED | CANCELLED
    waiting_for_approval: bool
    results: list[tuple[ToolCall, ToolResult]]


def run_agent(conn: sqlite3.Connection, run: Run, llm: LLM, *, campaign_id: str | None, task: str, max_steps: int = 20) -> AgentOutcome:
    transcript = Transcript()
    transcript.add("user", task)
    tools = [t.as_llm_tool() for t in registry.all_tools()]
    results: list[tuple[ToolCall, ToolResult]] = []
    for _ in range(max_steps):
        if stop_reason(conn, run):
            return AgentOutcome("CANCELLED", False, results)
        calls = llm.next_calls(transcript, tools)
        if not calls:
            return AgentOutcome("SUCCEEDED", _waiting_for_approval(results), results)
        transcript.add("assistant", [{"tool": c.name, "arguments": c.arguments} for c in calls])
        for call in calls:
            result = run_tool(conn, run, run.workspace_id, campaign_id, call.name, call.arguments)
            results.append((call, result))
            transcript.add("tool", {"tool": call.name, "result": result.for_model()})
            # A campaign created in this run becomes the run's campaign from here on.
            if call.name == "campaign_maker" and result.output:
                campaign_id = str(result.output["campaign_id"])
    return AgentOutcome("FAILED", False, results)


def _waiting_for_approval(results: list[tuple[ToolCall, ToolResult]]) -> bool:
    """Some gated action was held for the owner and not carried out later in the same run."""
    key = lambda c: (c.name, dumps(c.arguments))  # noqa: E731
    held = {key(c) for c, r in results if r.status == "CANCELLED" and (r.error or "").startswith(("APPROVAL_REQUIRED", "APPROVAL_PENDING"))}
    done = {key(c) for c, r in results if r.status == "SUCCEEDED"}
    return bool(held - done)


def work_once(
    conn: sqlite3.Connection,
    conn_factory: Callable[[], sqlite3.Connection],
    worker_id: str,
    llm_for: Callable[[Run], LLM],
    *,
    lease: float = 60.0,
    heartbeat_interval: float = 20.0,
) -> AgentOutcome | None:
    """Claim one run, drive the agent under a heartbeat, record the outcome. None if nothing to do."""
    run = claim(conn, worker_id, lease)
    if run is None:
        return None
    work = conn.execute("SELECT campaign_id, description FROM work_items WHERE work_id = ?", (run.work_id,)).fetchone()
    with Heartbeat(conn_factory, run, worker_id, heartbeat_interval, lease) as hb:
        try:
            outcome = run_agent(conn, run, llm_for(run), campaign_id=work["campaign_id"], task=work["description"])
        except Exception:
            finish(conn, run, worker_id, "FAILED")
            raise
    if hb.lost.is_set():
        # Another worker re-claimed this run after our lease lapsed; it owns the outcome now.
        return outcome
    finish(conn, run, worker_id, outcome.status, pause_for_approval=outcome.waiting_for_approval)
    return outcome
