"""Node: summarize — assemble the final onboarding report.

The LLM writes ONLY a short executive narrative + per-file recommendations (cheap,
guardrail-safe). The detailed reference tables are rendered deterministically in
services.report. We stitch: header + verdict + narrative + tables.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from agent.core.llm import ainvoke_text, make_llm
from agent.models.schemas import (
    FileLevelMapping,
    FileReadiness,
    MissingFile,
    OnboardingSummary,
    SheetCatalogEntry,
    SheetGapReport,
)
from agent.prompts.system_prompts import SUMMARIZER_SYSTEM
from agent.prompts.user_prompts import build_summarizer_prompt
from agent.services.report import build_compact_summary, render_tables


def _verdict(total_missing_mandatory: int, matched: int) -> str:
    if matched == 0:
        return "NOT READY"
    if total_missing_mandatory == 0:
        return "READY"
    if total_missing_mandatory <= 5:
        return "NEEDS ATTENTION"
    return "NOT READY"


async def summarize(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """Build the final report (LLM narrative + deterministic tables) and emit a message."""
    errors = list(state.get("errors") or [])
    market = state.get("market", "Unknown")
    gap_reports = state.get("gap_reports") or []
    profiles = state.get("source_profiles") or []
    file_mappings = state.get("file_mappings") or []
    missing_files = state.get("missing_files") or []
    catalog_entries = state.get("catalog_entries") or []
    file_readiness_list = state.get("file_readiness") or []
    catalog_csv_path = state.get("catalog_csv_path") or ""
    mapping_csv_path = state.get("mapping_csv_path") or ""

    reports = [SheetGapReport(**r) for r in gap_reports]
    total_missing_mandatory = sum(len(r.missing_mandatory) for r in reports)
    files_analyzed = len({p["file_name"] for p in profiles})
    verdict = _verdict(total_missing_mandatory, len(reports))

    # 1) Short, cheap LLM narrative from a compact summary.
    compact = build_compact_summary(
        market,
        file_mappings=file_mappings,
        gap_reports=gap_reports,
        missing_files=missing_files,
        source_profiles=profiles,
        file_readiness=file_readiness_list,
    )
    narrative = ""
    try:
        ctx = getattr(runtime, "context", None) or {}
        llm = make_llm(model=ctx.get("model"), max_tokens=ctx.get("max_tokens"))
        narrative = await ainvoke_text(llm, SUMMARIZER_SYSTEM, build_summarizer_prompt(market, compact))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"summarize: {exc}")
        narrative = _fallback_narrative(compact, verdict)

    # 2) Deterministic reference tables (accurate, free, guardrail-safe).
    tables = render_tables(
        market,
        file_mappings=file_mappings,
        gap_reports=gap_reports,
        missing_files=missing_files,
        source_profiles=profiles,
        catalog_csv_path=catalog_csv_path,
        mapping_csv_path=mapping_csv_path,
        file_readiness=file_readiness_list,
    )

    # 3) Stitch the full report.
    report = (
        f"# New Market Onboarding — {market}\n"
        f"**Verdict: {verdict}**\n\n"
        f"{narrative.strip()}\n\n"
        f"---\n\n"
        f"{tables}"
    )

    summary = OnboardingSummary(
        market=market,
        files_analyzed=files_analyzed,
        sheets_analyzed=len(profiles),
        domain_sheets_matched=len(reports),
        total_missing_mandatory=total_missing_mandatory,
        readiness_verdict=verdict,
        narrative=report,
        per_sheet=reports,
        file_readiness=[FileReadiness(**fr) for fr in file_readiness_list],
        catalog_entries=[SheetCatalogEntry(**e) for e in catalog_entries],
        file_mappings=[FileLevelMapping(**m) for m in file_mappings],
        missing_files=[MissingFile(**m) for m in missing_files],
        catalog_csv_path=catalog_csv_path,
        mapping_csv_path=mapping_csv_path,
    )

    return {
        "summary": summary.model_dump(),
        "messages": [AIMessage(content=report)],
        "errors": errors,
    }


def _fallback_narrative(compact: dict, verdict: str) -> str:
    """Deterministic narrative when the LLM call fails (tables are added separately)."""
    files = compact.get("files", [])
    matched = [f for f in files if f.get("maps_to")]
    lines = [
        f"Onboarding analysis for **{compact.get('market')}** — verdict **{verdict}**.",
        f"- Files analysed: {compact.get('files_analyzed')}, sheets: {compact.get('sheets_analyzed')}.",
        f"- Files matched to a template domain: {len(matched)} of {len(files)}.",
        f"- Total missing mandatory fields: {compact.get('total_missing_mandatory')}.",
    ]
    missing = compact.get("missing_domains", [])
    if missing:
        lines.append(f"- Missing domains: {', '.join(missing[:10])}.")
    lines.append("")
    lines.append("See the tables below for file-level mappings, rename recommendations and gaps.")
    return "\n".join(lines)
