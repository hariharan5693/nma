"""Node: gap_analysis — deterministic matched / unmapped / missing computation.

For each matched (uploaded sheet → domain sheet) pair, applies the confidence
threshold and classifies:
  - mapped_fields           : standard fields with a confident source column
  - unmapped_source_columns : source columns that mapped to nothing
  - missing_mandatory/C/O   : standard fields with no confident mapping, by flag
"""

from __future__ import annotations

from typing import Any

from agent.core.settings import get_settings
from agent.models.schemas import (
    ColumnMapping,
    FileClassification,
    SheetGapReport,
    SourceSheetProfile,
    TemplateSchema,
)


def _profile_key(file_name: str, sheet_name: str) -> str:
    return f"{file_name}::{sheet_name}"


async def gap_analysis(state: dict[str, Any]) -> dict[str, Any]:
    """Compute per-sheet gap reports from classifications + column mappings."""
    errors = list(state.get("errors") or [])
    threshold = get_settings().confidence_threshold

    template = TemplateSchema(**(state.get("template") or {}))
    profiles = {
        _profile_key(p["file_name"], p["sheet_name"]): SourceSheetProfile(**p)
        for p in (state.get("source_profiles") or [])
    }
    classifications = [FileClassification(**c) for c in (state.get("classifications") or [])]
    all_mappings = state.get("column_mappings") or {}

    reports: list[dict[str, Any]] = []
    for cls in classifications:
        if not cls.matched_domain_sheet:
            continue
        domain_sheet = template.get_sheet(cls.matched_domain_sheet)
        profile = profiles.get(_profile_key(cls.file_name, cls.sheet_name))
        if domain_sheet is None or profile is None:
            continue

        key = _profile_key(cls.file_name, cls.sheet_name)
        raw_mappings = [ColumnMapping(**m) for m in all_mappings.get(key, [])]

        # Keep only confident mappings; one standard field counts once.
        confident = [m for m in raw_mappings if m.confidence >= threshold]
        mapped_field_names = {m.standard_field_name for m in confident}
        mapped_source_cols = {m.source_column for m in confident}

        # Missing standard fields, split by flag.
        missing_m, missing_c, missing_o = [], [], []
        for field in domain_sheet.fields:
            if field.standard_field_name in mapped_field_names:
                continue
            if field.flag == "M":
                missing_m.append(field.standard_field_name)
            elif field.flag == "C":
                missing_c.append(field.standard_field_name)
            else:
                missing_o.append(field.standard_field_name)

        # Source columns that mapped to nothing confident.
        unmapped_cols = [c for c in profile.column_names() if c not in mapped_source_cols]

        reports.append(
            SheetGapReport(
                domain_sheet=domain_sheet.sheet_name,
                functional_area=domain_sheet.functional_area,
                source_file=cls.file_name,
                source_sheet=cls.sheet_name,
                mapped_fields=confident,
                unmapped_source_columns=unmapped_cols,
                missing_mandatory=missing_m,
                missing_conditional=missing_c,
                missing_optional=missing_o,
            ).model_dump()
        )

    return {"gap_reports": reports, "errors": errors}
