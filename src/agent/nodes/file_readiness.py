"""Node: file_readiness — deterministic per-file readiness scoring.

For every uploaded file, compute — with explicit references — how ready it is:
  - vs the GLOBAL TEMPLATE (the mapping target): mandatory-coverage % and all-field
    coverage % of the matched domain sheet, plus the list of missing mandatory fields.
  - vs the INDIA reference (how a mature, already-live market structures this domain):
    parity % = share of India's real columns that are represented by a confident mapping.

No LLM calls. This is the quantitative backbone of the file-by-file report.
"""

from __future__ import annotations

import difflib
from typing import Any

from agent.core.settings import get_settings
from agent.models.schemas import (
    ColumnMapping,
    FileClassification,
    FileReadiness,
    SourceSheetProfile,
    TemplateSchema,
)
from agent.services.reference import india_columns_for_domain


def _profile_key(file_name: str, sheet_name: str) -> str:
    return f"{file_name}::{sheet_name}"


def _norm_col(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _parity_vs_india(mapped_source_cols: set[str], india_cols: list[str]) -> float:
    """% of India reference columns covered by a confident mapped source column.

    We count an India column as 'covered' if a mapped source column matches it by
    fuzzy name similarity (>= 0.8) — a lightweight parity proxy.
    """
    if not india_cols:
        return 0.0
    norm_mapped = [_norm_col(c) for c in mapped_source_cols]
    covered = 0
    for icol in india_cols:
        ni = _norm_col(icol)
        if not ni:
            continue
        hit = any(
            ni == nm or ni in nm or nm in ni
            or difflib.SequenceMatcher(None, ni, nm).ratio() >= 0.8
            for nm in norm_mapped
        )
        if hit:
            covered += 1
    return round(100.0 * covered / len(india_cols), 1)


async def file_readiness(state: dict[str, Any]) -> dict[str, Any]:
    """Compute per-file readiness vs the global template and vs the India reference."""
    errors = list(state.get("errors") or [])
    threshold = get_settings().confidence_threshold

    template = TemplateSchema(**(state.get("template") or {}))
    profiles = {
        _profile_key(p["file_name"], p["sheet_name"]): SourceSheetProfile(**p)
        for p in (state.get("source_profiles") or [])
    }
    classifications = [FileClassification(**c) for c in (state.get("classifications") or [])]
    all_mappings = state.get("column_mappings") or {}

    # Aggregate at the FILE level (a file may contain multiple matched sheets).
    by_file: dict[str, FileReadiness] = {}

    for cls in classifications:
        key = _profile_key(cls.file_name, cls.sheet_name)
        profile = profiles.get(key)
        if profile is None:
            continue

        fr = by_file.get(cls.file_name)
        if fr is None:
            fr = FileReadiness(uploaded_file=cls.file_name)
            by_file[cls.file_name] = fr

        fr.source_column_count += profile.column_count

        if not cls.matched_domain_sheet:
            if not fr.match_basis:
                fr.match_basis = "No confident domain match — needs manual review."
            continue

        domain_sheet = template.get_sheet(cls.matched_domain_sheet)
        if domain_sheet is None:
            continue

        # Record the primary match on the file (first/strongest matched sheet).
        if not fr.matched_domain_sheet:
            fr.matched_domain_sheet = domain_sheet.sheet_name
            fr.functional_area = domain_sheet.functional_area
            fr.match_confidence = round(cls.confidence, 2)
            fr.match_basis = cls.reasoning or "Matched on business meaning + columns."
            fr.compared_against_template = f"Global Template: {domain_sheet.sheet_name}"

        raw_mappings = [ColumnMapping(**m) for m in all_mappings.get(key, [])]
        confident = [m for m in raw_mappings if m.confidence >= threshold]
        mapped_field_names = {m.standard_field_name for m in confident}
        mapped_source_cols = {m.source_column for m in confident}

        fr.mapped_column_count += len(mapped_source_cols)

        # Coverage vs the GLOBAL TEMPLATE domain sheet.
        mandatory = [f for f in domain_sheet.fields if f.flag == "M"]
        fr.template_total_fields += len(domain_sheet.fields)
        fr.template_total_mandatory += len(mandatory)
        fr.template_mapped_fields += sum(
            1 for f in domain_sheet.fields if f.standard_field_name in mapped_field_names
        )
        fr.template_mapped_mandatory += sum(
            1 for f in mandatory if f.standard_field_name in mapped_field_names
        )
        for f in mandatory:
            if f.standard_field_name not in mapped_field_names:
                fr.missing_mandatory.append(f.standard_field_name)

        # Parity vs the INDIA reference columns for this domain.
        india_cols = india_columns_for_domain(domain_sheet.sheet_name)
        if india_cols and not fr.india_reference_file:
            fdm = template.functional_domain_map.get(domain_sheet.sheet_name, {})
            fr.india_reference_file = fdm.get("INDIA", "") or f"{domain_sheet.sheet_name} (India)"
            fr.india_reference_column_count = len(india_cols)
            fr.compared_against_india = f"India reference: {fr.india_reference_file}"
            fr.parity_pct_vs_india = _parity_vs_india(mapped_source_cols, india_cols)

        # Transformation recommendations (from the confident mappings).
        for m in confident:
            if m.transform_action in {"RENAME", "KEEP"}:
                continue  # trivial — don't clutter the headline recs
            note = m.transform_note or f"{m.transform_action} {m.source_column}"
            if note not in fr.transformations:
                fr.transformations.append(note)

    # Finalise percentages + headline transformations.
    results: list[dict[str, Any]] = []
    for fr in by_file.values():
        if fr.template_total_mandatory:
            fr.ready_pct_vs_template = round(
                100.0 * fr.template_mapped_mandatory / fr.template_total_mandatory, 1
            )
        if fr.template_total_fields:
            fr.ready_pct_vs_template_all = round(
                100.0 * fr.template_mapped_fields / fr.template_total_fields, 1
            )
        # De-duplicate missing mandatory.
        fr.missing_mandatory = sorted(set(fr.missing_mandatory))

        # Add standard transformation headlines.
        if fr.matched_domain_sheet:
            if fr.ready_pct_vs_template < 100 and fr.missing_mandatory:
                fr.transformations.insert(
                    0,
                    f"Add/derive {len(fr.missing_mandatory)} missing mandatory field(s): "
                    + ", ".join(fr.missing_mandatory[:6])
                    + ("…" if len(fr.missing_mandatory) > 6 else ""),
                )
        else:
            fr.transformations = [
                "Provide a clearer business description / confirm the domain — "
                "no confident template match yet."
            ]
        results.append(fr.model_dump())

    # Stable order: matched first (by readiness desc), then unmatched.
    results.sort(key=lambda r: (r["matched_domain_sheet"] == "", -r["ready_pct_vs_template"]))

    return {"file_readiness": results, "errors": errors}
