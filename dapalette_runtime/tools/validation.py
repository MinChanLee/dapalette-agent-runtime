"""Input contract check, run before any handler.

The declared parameter schema is the contract. Violations are caught here for every tool, and the
error message returns the declared argument list so the model can correct itself on the next step.
(Observed: a call shaped {"fields": "<json string>"} was silently accepted by a handler, and the model
had no way to learn its arguments were wrong.)
"""

from __future__ import annotations

from typing import Any

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _PY_TYPES[expected])


def validate(schema: dict[str, Any], data: dict[str, Any]) -> str | None:
    props: dict[str, Any] = schema.get("properties", {})
    problems: list[str] = []
    for name in schema.get("required", []):
        if name not in data or data[name] in (None, ""):
            problems.append(f"missing required '{name}'")
    for name, value in data.items():
        spec = props.get(name)
        if spec is None:
            problems.append(f"unknown argument '{name}'")
            continue
        if not _type_ok(value, spec["type"]):
            problems.append(f"'{name}' must be {spec['type']}, got {type(value).__name__}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"'{name}' must be one of {spec['enum']}")
        if spec["type"] == "array" and "items" in spec:
            bad = [v for v in value if not _type_ok(v, spec["items"]["type"])]
            if bad:
                problems.append(f"'{name}' items must be {spec['items']['type']}")
        if spec["type"] in ("integer", "number") and "minimum" in spec and value < spec["minimum"]:
            problems.append(f"'{name}' must be ≥ {spec['minimum']}")
    if not problems:
        return None
    declared = ", ".join(f"{n}: {p['type']}{' (required)' if n in schema.get('required', []) else ''}" for n, p in props.items())
    return f"INVALID_INPUT: {'; '.join(problems)}. Declared arguments: {declared}"
