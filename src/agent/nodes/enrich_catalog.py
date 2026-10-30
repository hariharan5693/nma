"""Node: enrich_catalog — [LLM] build India-style catalog rows for each matched sheet.

For every uploaded sheet that matched a template domain sheet, produce one enriched
catalog entry (mirroring raw_intents_enhanced_v2.csv) PLUS a `possible_cut` column.
Uses a business-equivalent India catalog row as a few-shot style exemplar.
Runs one LLM call per matched sheet, concurrently.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from langgraph.runtime import Runtime

from agent.core.llm import ainvoke_json, make_fast_llm
from agent.models.schemas import (
    FileClassification,
    SheetCatalogEntry,
    SourceSheetProfile,
    TemplateSchema,
)
from agent.prompts.system_prompts import CATALOG_ENRICHER_SYSTEM
from agent.prompts.user_prompts import build_catalog_enricher_prompt
from agent.services.reference import POSSIBLE_CUT_VOCAB, india_catalog_example


def _profile_key(file_name: str, sheet_name: str) -> str:
    return f"{file_name}::{sheet_name}"


def _fallback_file_id(domain_sheet: str, sheet_name: str) -> str:
    base = (domain_sheet or sheet_name or "SHEET").upper()
    base = re.sub(r"[^A-Z0-9]+", "_", base).strip("_")
    return base[:40] or "SHEET"


# Country name/code tokens the LLM tends to bake into file_id (e.g. from India
# exemplars or filenames). We strip ALL of these and re-prefix with the real market.
_COUNTRY_TOKENS = {
    "TH", "THAI", "THAILAND", "IN", "IND", "INDIA", "VN", "VND", "VIET", "VIETNAM",
    "PH", "PHL", "PHILIPPINES", "ID", "IDN", "INDONESIA", "MY", "MYS", "MALAYSIA",
    "JP", "JPN", "JAPAN", "KR", "KOR", "KOREA",
}

# Market name -> short country code used as the file_id prefix.
_MARKET_CODE = {
    "THAILAND": "TH", "INDIA": "IN", "VIETNAM": "VN", "PHILIPPINES": "PH",
    "INDONESIA": "ID", "MALAYSIA": "MY", "JAPAN": "JP", "KOREA": "KR",
}


def _market_code(market: str) -> str:
    m = (market or "").strip().upper()
    if m in _MARKET_CODE:
        return _MARKET_CODE[m]
    # Fall back to the first alphanumeric token, capped at 3 chars.
    tok = re.sub(r"[^A-Z0-9]+", "", m)[:3]
    return tok or "MKT"


def _normalize_file_id(raw_id: str, domain_sheet: str, sheet_name: str, market: str) -> str:
    """Make the File_ID market-correct and deterministic.

    Strips any country tokens the LLM injected (e.g. '_TH', 'THAILAND_') and
    prefixes the id with the ACTUAL market's country code, so a Vietnam run yields
    e.g. 'VN_DELIVERY_SHIPMENT' rather than 'DELIVERY_SHIPMENT_TH'.
    """
    base = (raw_id or "").upper()
    base = re.sub(r"[^A-Z0-9]+", "_", base).strip("_")
    parts = [p for p in base.split("_") if p and p not in _COUNTRY_TOKENS]
    if not parts:
        fb = _fallback_file_id(domain_sheet, sheet_name)
        parts = [p for p in fb.split("_") if p and p not in _COUNTRY_TOKENS] or ["SHEET"]
    code = _market_code(market)
    return f"{code}_{'_'.join(parts)}"[:48]


def _clean_cuts(value: str) -> str:
    """Keep only allowed cut vocabulary, de-duplicated, semicolon-joined."""
    if not value:
        return ""
    found: list[str] = []
    low = value.lower()
    for term in POSSIBLE_CUT_VOCAB:
        if term.lower() in low and term not in found:
            found.append(term)
    return "; ".join(found)


def _derive_cuts_from_columns(profile: SourceSheetProfile) -> str:
    """Heuristic fallback: infer cuts from column names if the LLM omitted them."""
    cols = " ".join(profile.column_names()).lower()
    hits: list[str] = []
    rules = {
        "Geography": ["market", "region", "province", "city", "zone", "state", "town", "geo"],
        "Variant": ["variant", "ebrom", "marketed brand"],
        "SKU": ["sku", "product code", "productcode", "pack"],
        "Segment": ["segment", "price segment", "category", "length", "format", "strength"],
        "Brand": ["brand"],
        "Channel": ["channel", "outlet", "store", "dealer", "account"],
        "Time": ["period", "month", "year", "jan", "feb", "quarter", "date"],
    }
    for cut, kws in rules.items():
        if any(k in cols for k in kws):
            hits.append(cut)
    return "; ".join(hits)


async def _enrich_one(llm, market, domain_sheet, profile) -> tuple[dict[str, Any] | None, str | None]:
    key = _profile_key(profile.file_name, profile.sheet_name)
    example = india_catalog_example(domain_sheet.sheet_name)
    try:
        prompt = build_catalog_enricher_prompt(market, domain_sheet, profile, example)
        raw = await ainvoke_json(llm, CATALOG_ENRICHER_SYSTEM, prompt)
        if not isinstance(raw, dict):
            raw = {}

        cuts = _clean_cuts(str(raw.get("possible_cut", ""))) or _derive_cuts_from_columns(profile)
        entry = SheetCatalogEntry(
            file_id=_normalize_file_id(
                str(raw.get("file_id", "")), domain_sheet.sheet_name,
                profile.sheet_name, market,
            ),
            file=profile.file_name,
            sheet=profile.sheet_name,
            expressed_in=str(raw.get("expressed_in", "")),
            units=str(raw.get("units", "")),
            actual_columns=" | ".join(profile.column_names()),
            description=str(raw.get("description", "")),
            business_facts=str(raw.get("business_facts", "")),
            granularity_type=str(raw.get("granularity_type", "Raw") or "Raw"),
            possible_cut=cuts,
            domain_sheet=domain_sheet.sheet_name,
        )
        return entry.model_dump(), None
    except Exception as exc:  # noqa: BLE001
        # Deterministic fallback so a failed LLM call still yields a usable row.
        entry = SheetCatalogEntry(
            file_id=_normalize_file_id(
                "", domain_sheet.sheet_name, profile.sheet_name, market,
            ),
            file=profile.file_name,
            sheet=profile.sheet_name,
            actual_columns=" | ".join(profile.column_names()),
            description=f"Sheet '{profile.sheet_name}' from '{profile.file_name}' "
            f"matched to template domain '{domain_sheet.sheet_name}'.",
            possible_cut=_derive_cuts_from_columns(profile),
            domain_sheet=domain_sheet.sheet_name,
        )
        return entry.model_dump(), f"enrich_catalog[{key}]: {exc}"


async def enrich_catalog(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """Produce an India-style enriched catalog entry per matched sheet."""
    errors = list(state.get("errors") or [])
    template = TemplateSchema(**(state.get("template") or {}))
    profiles = {
        _profile_key(p["file_name"], p["sheet_name"]): SourceSheetProfile(**p)
        for p in (state.get("source_profiles") or [])
    }
    classifications = [FileClassification(**c) for c in (state.get("classifications") or [])]

    ctx = getattr(runtime, "context", None) or {}
    market = state.get("market", "Unknown")
    llm = make_fast_llm(model=ctx.get("model"))

    tasks = []
    for cls in classifications:
        if not cls.matched_domain_sheet:
            continue
        domain_sheet = template.get_sheet(cls.matched_domain_sheet)
        profile = profiles.get(_profile_key(cls.file_name, cls.sheet_name))
        if domain_sheet is None or profile is None:
            continue
        tasks.append(_enrich_one(llm, market, domain_sheet, profile))

    entries: list[dict[str, Any]] = []
    if tasks:
        results = await asyncio.gather(*tasks)
        for entry, err in results:
            if entry:
                entries.append(entry)
            if err:
                errors.append(err)

    return {"catalog_entries": entries, "errors": errors}
