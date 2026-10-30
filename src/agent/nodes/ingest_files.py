"""Node: ingest_files — resolve inputs and profile every uploaded sheet.

Inputs can arrive two ways:
  1. Explicit state keys `market` and `uploaded_files` (CLI / Streamlit / direct graph call).
  2. A JSON payload embedded in the latest human message (A2A / LiteLLM gateway call),
     shaped like: {"market": "...", "uploaded_files": [{"name","path"|"content_b64"}]}

This node normalises both into state and produces source_profiles.
"""

from __future__ import annotations

import json
from typing import Any

from agent.services.excel_reader import profile_upload


def _parse_message_payload(messages: list) -> dict[str, Any]:
    """Try to pull a JSON onboarding payload out of the last human message."""
    if not messages:
        return {}
    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content")
    if not isinstance(content, str):
        return {}
    content = content.strip()
    if not (content.startswith("{") and content.endswith("}")):
        return {}
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def ingest_files(state: dict[str, Any]) -> dict[str, Any]:
    """Resolve market + uploaded_files, then profile each uploaded sheet."""
    errors = list(state.get("errors") or [])

    market = state.get("market")
    uploaded_files = state.get("uploaded_files")

    # Fall back to a JSON payload inside the latest message (A2A path).
    if not uploaded_files:
        payload = _parse_message_payload(state.get("messages") or [])
        market = market or payload.get("market")
        uploaded_files = payload.get("uploaded_files") or []

    market = market or "Unknown"
    uploaded_files = uploaded_files or []

    profiles: list[dict[str, Any]] = []
    for upload in uploaded_files:
        try:
            for prof in profile_upload(upload):
                profiles.append(prof.model_dump())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ingest_files[{upload.get('name', '?')}]: {exc}")

    if not profiles and not errors:
        errors.append("ingest_files: no uploaded files were provided or profiled.")

    return {
        "market": market,
        "uploaded_files": uploaded_files,
        "source_profiles": profiles,
        "errors": errors,
    }
