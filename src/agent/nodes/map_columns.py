"""Node: map_columns — [LLM] map source columns to standard fields, per matched sheet.

Runs one LLM call per (uploaded sheet → matched domain sheet) pair, concurrently.
Produces column_mappings keyed by "file_name::sheet_name".
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.runtime import Runtime

from agent.core.llm import ainvoke_json, make_llm
from agent.models.schemas import (
    ColumnMapping,
    FileClassification,
    SourceSheetProfile,
    TemplateSchema,
)
from agent.prompts.system_prompts import MAPPER_SYSTEM
from agent.prompts.user_prompts import build_mapper_prompt
from agent.services.reference import india_columns_for_domain


def _profile_key(file_name: str, sheet_name: str) -> str:
    return f"{file_name}::{sheet_name}"


_VALID_ACTIONS = {"RENAME", "KEEP", "SPLIT", "DERIVE", "UNPIVOT", "DROP", "REVIEW"}


async def _map_one(llm, market, domain_sheet, profile, india_columns) -> tuple[str, list[dict[str, Any]], str | None]:
    key = _profile_key(profile.file_name, profile.sheet_name)
    try:
        prompt = build_mapper_prompt(market, domain_sheet, profile, india_columns)
        raw = await ainvoke_json(llm, MAPPER_SYSTEM, prompt)
        # Template field lookup for back-filling business_description + flag.
        field_by_name = {f.standard_field_name: f for f in domain_sheet.fields}
        valid_cols = set(profile.column_names())
        mappings: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            sfn = item.get("standard_field_name")
            sc = item.get("source_column")
            if sfn not in field_by_name or sc not in valid_cols:
                continue  # drop hallucinated field/column
            field = field_by_name[sfn]
            action = str(item.get("transform_action", "RENAME")).upper()
            if action not in _VALID_ACTIONS:
                action = "RENAME"
            # If the source column already equals the standard name, it's KEEP.
            if action == "RENAME" and sc.strip().lower() == sfn.strip().lower():
                action = "KEEP"
            note = item.get("transform_note", "")
            if not note:
                note = (
                    f"Rename '{sc}' to '{sfn}'" if action == "RENAME"
                    else f"{action.title()} '{sc}' -> '{sfn}'"
                )
            mappings.append(
                ColumnMapping(
                    source_column=sc,
                    standard_field_name=sfn,
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    reasoning=item.get("reasoning", ""),
                    business_description=field.business_description,
                    field_flag=field.flag,
                    transform_action=action,
                    transform_note=note,
                ).model_dump()
            )
        return key, mappings, None
    except Exception as exc:  # noqa: BLE001
        return key, [], f"map_columns[{key}]: {exc}"


async def map_columns(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """Map columns for every uploaded sheet that got a confident domain match."""
    errors = list(state.get("errors") or [])
    template = TemplateSchema(**(state.get("template") or {}))
    profiles = {
        _profile_key(p["file_name"], p["sheet_name"]): SourceSheetProfile(**p)
        for p in (state.get("source_profiles") or [])
    }
    classifications = [FileClassification(**c) for c in (state.get("classifications") or [])]

    ctx = getattr(runtime, "context", None) or {}
    market = state.get("market", "Unknown")
    llm = make_llm(model=ctx.get("model"), max_tokens=ctx.get("max_tokens"))

    tasks = []
    for cls in classifications:
        if not cls.matched_domain_sheet:
            continue
        domain_sheet = template.get_sheet(cls.matched_domain_sheet)
        profile = profiles.get(_profile_key(cls.file_name, cls.sheet_name))
        if domain_sheet is None or profile is None:
            continue
        india_cols = india_columns_for_domain(domain_sheet.sheet_name)
        tasks.append(_map_one(llm, market, domain_sheet, profile, india_cols))

    column_mappings: dict[str, list[dict[str, Any]]] = {}
    if tasks:
        results = await asyncio.gather(*tasks)
        for key, mappings, err in results:
            column_mappings[key] = mappings
            if err:
                errors.append(err)

    return {"column_mappings": column_mappings, "errors": errors}
