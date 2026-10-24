"""Tool registry: one declaration per tool — schema, side-effect class, idempotency identity, handler.

Keeping these together is the point. Production had a defect class where the schema shown to the model,
the handler's actual arguments and the DB columns drifted apart (a field typed as a string silently
flattened a structured value). A single declaration plus parity tests closed it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import dumps


class Effect(Enum):
    READ = "read"  # no side effects: never gated, never deduplicated
    WRITE = "write"  # internal state change
    EXTERNAL = "external"  # leaves the system: email, signing links, carrier requests
    MONEY = "money"  # moves money


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    workspace_id: str
    work_id: str | None
    run_id: str | None
    campaign_id: str | None


Handler = Callable[[dict[str, Any], ToolContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: Effect
    handler: Handler
    #: input fields that identify "the same action" — the rest (wording, notes) does not make it a new action
    idem_fields: tuple[str, ...] = ()
    #: only runnable once the campaign is ACTIVE (not while it is still being planned)
    needs_active_campaign: bool = False
    #: False for calls that are safe to repeat (posting a chat message, asking for an approval card)
    dedupe: bool = True

    def as_llm_tool(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"duplicate tool {spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec:
    return _REGISTRY[name]


def all_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def campaign_of(input_data: dict[str, Any], ctx_campaign_id: str | None) -> str | None:
    """Which campaign a call acts on: the explicit argument wins; the work's campaign is only a fallback."""
    explicit = input_data.get("campaign_id")
    return str(explicit) if explicit else ctx_campaign_id


def idem_key(spec: ToolSpec, input_data: dict[str, Any], ctx_campaign_id: str | None, workspace_id: str) -> str | None:
    """The identity of an action. Used for BOTH duplicate suppression and approval matching.

    There is exactly one implementation on purpose: when the approval card and the runner computed the
    key from different sources (the card from its payload, the runner from context), an approved action
    never matched its approval and the agent looped asking for approval forever.

    Only things that identify the action go in. A tool that acts on a campaign is keyed by that campaign;
    a tool that *creates* one is not — otherwise its key would change the moment it succeeded (the new
    campaign becomes the work's context) and a replay would create a second campaign.
    """
    if spec.effect is Effect.READ or not spec.dedupe:
        return None
    fields = spec.idem_fields or tuple(sorted(input_data))
    identity: dict[str, Any] = {"workspace": workspace_id}
    if "campaign_id" in spec.parameters.get("properties", {}):
        identity["campaign"] = campaign_of(input_data, ctx_campaign_id)
    for f in fields:
        value = input_data.get(f)
        identity[f] = sorted(value) if isinstance(value, list) else value
    digest = hashlib.sha256(dumps(identity).encode()).hexdigest()[:24]
    return f"{spec.name}:{digest}"
