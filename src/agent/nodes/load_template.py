"""Node: load_template — parse the bundled global template into the state."""

from __future__ import annotations

from typing import Any

from agent.services.template_parser import load_template_cached


async def load_template(state: dict[str, Any]) -> dict[str, Any]:
    """Parse the global template once and store its schema in state."""
    errors = list(state.get("errors") or [])
    try:
        schema = load_template_cached(None)
        return {"template": schema.model_dump()}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"load_template: {exc}")
        return {"template": {}, "errors": errors}
