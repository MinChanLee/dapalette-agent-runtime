# Dapalette — Agent Runtime for Influencer Marketing (excerpt)

Dapalette is an AI agent that runs a brand's influencer-marketing campaigns end to end — planning, finding creators, outreach, contracts, product shipping, reviewing the posts, and paying creators — with the brand owner stepping in only where their judgment or consent is actually needed.

**Live product: [dapalette.com](https://dapalette.com)** — a demo account is available on request.

This repository is an **excerpt of the runtime underneath that agent**: how an LLM that sends real emails, signs contracts and moves money is made safe to leave running. The marketing domain is kept, condensed to the eight tools a seeding campaign needs, so the runtime can be seen doing real work.

> Full project, for scale: FastAPI + async SQLAlchemy/PostgreSQL backend (~130k lines), Next.js owner app, ~70 agent tools, ~180 migrations, ~370 test files, ~2,250 commits since February 2026. I'm the main author (~2,280 commits); a teammate contributed the rest. What is left out here: the owner app, creator sourcing and profiling, negotiation, multi-country shipping, content rights, analytics, the prompt/context builder and the multi-provider LLM router.

---

## The problem

An LLM agent is a non-deterministic program that will, sooner or later: retry an action it already took, act on an instruction the owner has since changed, keep going after the owner said stop, call a tool with the wrong argument shapes, pay someone twice, or ask the owner to approve something that can't run anyway. Prompting it not to is not a guarantee. Each of those had to become **structurally impossible**, or at least caught before the side effect.

## Architecture

```
owner chat / creator email / scheduler
        │  append to event_store
        ▼
Control Plane (deterministic, no LLM) ── "is there live work for this conversation?"
        │  yes → bump work version      no → create work v1
        ▼
worker_runs (PENDING) ──► Worker: claim with lease ── heartbeat ── re-claim if the holder dies
                                   │
                                   ▼
                          Agent loop (native tool calling)
                                   │  every call
                                   ▼
                          ToolRunner: ① INSERT with idem_key  ② checklist  ③ RUNNING  ④ result
                                   │
                                   ▼
                          Marketing tools — the agent judges, the code enforces
```

### Control Plane — deterministic routing ([`control_plane.py`](dapalette_runtime/control_plane.py))
Early on, an LLM routed each inbound message (respond? cancel? which campaign?). That was unreproducible and sometimes attached work to the wrong campaign, so routing became plain code with one question. A new message on a live conversation **bumps the work's version**; the older run notices it has been superseded and stops, and the new run starts with the newer instruction.

### Worker — leases, not locks ([`worker.py`](dapalette_runtime/worker.py))
One atomic `UPDATE … RETURNING` claims a run only if: it's the active version, the work isn't terminal, no other run of that work is RUNNING, and every dependency is COMPLETED. A heartbeat renews the lease; an expired lease is re-claimed by another worker, and the old holder can no longer extend or finish it. Rules that came from incidents are noted in the code — e.g. a run left over from a cancelled work was claimed a month later and acted in the real world, so terminal work is excluded from the claim itself.

### ToolRunner — one path for every side effect ([`tools/runner.py`](dapalette_runtime/tools/runner.py))
1. **Idempotency by schema.** Each call is inserted with an `idem_key`; a partial unique index makes a second identical action impossible, not merely unlikely. Blocked attempts are still recorded. FAILED/CANCELLED calls release their key so a corrected retry can run.
2. **Checklist, fundamental first:** input contract (errors list the declared arguments so the model self-corrects) → is this run still current (version/cancellation) → is the campaign ACTIVE → owner approval. Activation is checked *before* approval so the owner is never asked to approve something that can't run.
3. **Approvals match an exact action, not a tool.** The approval card and the runner use the same `idem_key` function — when they didn't, approved actions never matched and the agent asked forever. Unanswered cards expire and the agent proceeds, except money and irreversible tools, which stay blocked (a *block list*, so new tools default to "don't freeze the campaign").
4. **Refusals steer.** Messages say what to do next ("ask what they want instead", "report the contradiction") — e.g. "already proceeded" explicitly tells the model the record is all it asserts, after a model read it as proof the effect existed.

### Marketing tools — the agent judges, the code enforces ([`marketing/tools.py`](dapalette_runtime/marketing/tools.py))
Which creators fit and how to phrase outreach is the agent's call. Stage order (contact → contract → ship → post → settle), the remaining budget, what a contract still owes, and the paid-partnership disclosure that Korean advertising rules require are enforced in code — **even after the owner approves** — and current numbers are returned so the model never has to recompute them.

## See it run

```bash
python demo.py      # no dependencies, no API key
```

A scripted stand-in plays the model and **replays its whole plan from the top on every run**, which is exactly the situation the runtime is for. Excerpt of the output:

```
━━ 3. Owner approves → the work wakes up. Run 2 replays the plan from the top:
      → campaign_maker {...}
      ← campaign_maker: CANCELLED: already proceeded — an identical action already succeeded …
      → campaign_starter {...}
      ← campaign_starter: {"status": "ACTIVE", ...}
━━ 5. A creator replies. Contracts always need approval — and approval never overrides the budget.
      ← contract_sender: FAILED: fee 3500000 exceeds the remaining budget 3000000 …
━━ 6. … a post without #광고 is not accepted …
      ← content_reviewer: {"accepted": false, "reason": "no paid-partnership disclosure …"}
━━ 7. Payment card goes unanswered and expires: other tools would proceed, money stays blocked.
      ← settlement_manager: CANCELLED: APPROVAL_EXPIRED: 'settlement_manager' moves money …
```

Building this excerpt reproduced one of the production bug classes in miniature: `campaign_maker`'s key first included the work's campaign — which `campaign_maker` itself sets — so the replay created a second campaign. The fix and its regression test are in [`registry.idem_key`](dapalette_runtime/tools/registry.py) and [`test_tool_runner.py`](tests/test_tool_runner.py).

## Tests

```bash
pip install -e ".[dev]" && pytest      # 26 tests: control plane, leases, concurrency, idempotency, approvals, domain rules
pyright                                # type check: 0 errors
sql/verify_postgres.sh                 # production claim query on a real PostgreSQL (PGHOST/PGPORT/PGUSER)
```

The runtime here uses stdlib SQLite so it runs anywhere; the invariants (partial unique indexes, atomic claim) are the same. The production claim adds `FOR UPDATE … SKIP LOCKED`; [`sql/postgres.sql`](sql/postgres.sql) has it, and the verify script checks it on PostgreSQL 16: 8 concurrent sessions drain 200 runs with each claimed exactly once, stale/terminal/blocked runs are never claimed, and the unique indexes reject a second RUNNING run and a duplicate `idem_key`.

## Layout

```
dapalette_runtime/
  db.py                 schema; invariants as partial unique indexes
  control_plane.py      events → versioned work (deterministic)
  worker.py             claim / lease / heartbeat / finish
  agent.py              tool-calling loop + one worker step
  tools/registry.py     tool declarations, effect classes, idempotency identity
  tools/validation.py   input contract with self-correcting errors
  tools/approvals.py    approval cards, expiry policy
  tools/runner.py       the four steps
  marketing/tools.py    campaign → outreach → contract → shipping → content → settlement
demo.py                 end-to-end walkthrough
sql/                    PostgreSQL claim + verification
tests/
```

---

© 2026 MinChan Lee. Published for review as part of a portfolio; not licensed for reuse.
