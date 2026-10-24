"""Dapalette agent runtime (excerpt): event → Control Plane → versioned work → leased worker
→ agent loop → ToolRunner (idempotency, checklist, approvals) → marketing tools."""

from .marketing.tools import register_all as _register_marketing_tools

_register_marketing_tools()
