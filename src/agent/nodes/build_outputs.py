"""Node: build_outputs — deterministic artefacts for the onboarding report.

No LLM calls. From the computed classifications / mappings / catalog entries it:
  1. Builds FILE-LEVEL mappings (uploaded file -> template domain(s)) with a
     rename recommendation toward the standard template file name.
  2. Detects MISSING files/domains (template business sheets with no match).
  3. Writes TWO CSVs into settings.outputs_dir:
       <market>_raw_intents_enhanced.csv   (India catalog shape + possible_cut)
       <market>_column_mapping.csv         (column-level mapping + transformation)
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from agent.core.settings import get_settings
from agent.models.schemas import (
    ColumnMapping,
    FileClassification,
    FileLevelMapping,
    MissingFile,
    SheetCatalogEntry,
    SourceSheetProfile,
    TemplateSchema,
)
from agent.services.reference import CATALOG_COLUMNS, POSSIBLE_CUT_COLUMN


def _profile_key(file_name: str, sheet_name: str) -> str:
    return f"{file_name}::{sheet_name}"


def _market_country_key(market: str) -> str:
    """Map a market name to the template's <COUNTRY> key used in functional_domain_map."""
    m = (market or "").strip().upper()
    # functional_domain_map keys come from "<COUNTRY>_FILES" headers (INDIA/THAILAND/VIETNAM/...).
    aliases = {
        "INDIA": "INDIA",
        "THAILAND": "THAILAND",
        "VIETNAM": "VIETNAM",
        "VN": "VIETNAM",
        "TH": "THAILAND",
        "IN": "INDIA",
    }
    return aliases.get(m, m)


def _safe_market_slug(market: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (market or "market").strip()).strip("_").lower()
    return slug or "market"


def _standard_file_name(
    template: TemplateSchema, domain_sheet: str, country_key: str
) -> str:
    """Best standard file name for a domain sheet: this market's, else India's, else any."""
    files = template.functional_domain_map.get(domain_sheet, {})
    if not files:
        return ""
    if country_key in files and files[country_key]:
        return files[country_key]
    if "INDIA" in files and files["INDIA"]:
        return files["INDIA"]
    # Fall back to the first non-empty known source file.
    for v in files.values():
        if v:
            return v
    return ""


def _build_file_mappings(
    template: TemplateSchema,
    profiles: dict[str, SourceSheetProfile],
    classifications: list[FileClassification],
    country_key: str,
) -> list[dict[str, Any]]:
    """One entry per uploaded file, aggregating its matched domain sheets."""
    # file -> sheets found
    sheets_by_file: dict[str, list[str]] = {}
    for p in profiles.values():
        sheets_by_file.setdefault(p.file_name, []).append(p.sheet_name)

    # file -> {domain_sheet: functional_area}
    domains_by_file: dict[str, dict[str, str]] = {}
    for cls in classifications:
        if not cls.matched_domain_sheet:
            continue
        ds = template.get_sheet(cls.matched_domain_sheet)
        area = ds.functional_area if ds else ""
        domains_by_file.setdefault(cls.file_name, {})[cls.matched_domain_sheet] = area

    results: list[dict[str, Any]] = []
    for file_name, sheets in sheets_by_file.items():
        domains = domains_by_file.get(file_name, {})
        domain_list = list(domains.keys())
        areas = sorted({a for a in domains.values() if a})

        # The STANDARD file name is the matched GLOBAL-TEMPLATE domain name
        # (the target the market must conform to), e.g. "PMI Secondary Volume.xlsx"
        # — NOT the raw per-country source filename from FUNCTIONAL_DOMAIN_MAP.
        primary_domain = domain_list[0] if domain_list else ""
        std_name = f"{primary_domain}.xlsx" if primary_domain else ""

        # The known per-country/India source filename is kept only as a hint.
        source_hint = ""
        for ds_name in domain_list:
            source_hint = _standard_file_name(template, ds_name, country_key)
            if source_hint:
                break

        rename = bool(std_name) and std_name.strip().lower() != file_name.strip().lower()
        if not domain_list:
            rec = "No confident template match — manual review needed."
        elif rename:
            rec = (
                f"Rename '{file_name}' -> '{std_name}' "
                f"(standard Global Template domain name)."
            )
            if source_hint and source_hint.strip().lower() != std_name.strip().lower():
                rec += f" Known source-file hint for this domain: '{source_hint}'."
        else:
            rec = "File name already matches the standard Global Template domain name."

        results.append(
            FileLevelMapping(
                uploaded_file=file_name,
                matched_domain_sheets=domain_list,
                functional_areas=areas,
                standard_file_name=std_name,
                rename_recommended=rename,
                rename_recommendation=rec,
                sheets_found=sheets,
            ).model_dump()
        )
    return results


def _build_missing_files(
    template: TemplateSchema,
    classifications: list[FileClassification],
    country_key: str,
) -> list[dict[str, Any]]:
    """Template business sheets (non-DIM) that no uploaded sheet matched."""
    matched = {c.matched_domain_sheet for c in classifications if c.matched_domain_sheet}
    missing: list[dict[str, Any]] = []
    for sheet in template.business_sheets():
        if sheet.sheet_name in matched:
            continue
        hint = _standard_file_name(template, sheet.sheet_name, country_key)
        expected = f"{sheet.sheet_name}.xlsx"
        if hint and hint.strip().lower() != expected.strip().lower():
            expected = f"{expected} (source hint: {hint})"
        missing.append(
            MissingFile(
                domain_sheet=sheet.sheet_name,
                functional_area=sheet.functional_area,
                expected_file_name=expected,
            ).model_dump()
        )
    return missing


def _write_catalog_csv(path: Path, entries: list[SheetCatalogEntry]) -> None:
    columns = CATALOG_COLUMNS + [POSSIBLE_CUT_COLUMN]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for e in entries:
            writer.writerow(
                [
                    e.file_id,
                    e.file,
                    e.sheet,
                    e.expressed_in,
                    e.units,
                    e.actual_columns,
                    e.description,
                    e.business_facts,
                    e.granularity_type,
                    e.possible_cut,
                ]
            )


def _write_mapping_csv(
    path: Path,
    profiles: dict[str, SourceSheetProfile],
    classifications: list[FileClassification],
    all_mappings: dict[str, list[dict[str, Any]]],
    threshold: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "file",
        "sheet",
        "domain_sheet",
        "source_column",
        "standard_field_name",
        "business_description",
        "flag",
        "confidence",
        "status",
        "transform_action",
        "transform_note",
    ]
    cls_by_key = {
        _profile_key(c.file_name, c.sheet_name): c for c in classifications
    }
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for key, profile in profiles.items():
            cls = cls_by_key.get(key)
            domain = cls.matched_domain_sheet if cls else ""
            raw_maps = [ColumnMapping(**m) for m in all_mappings.get(key, [])]
            mapped_by_col = {m.source_column: m for m in raw_maps if m.confidence >= threshold}

            for col in profile.column_names():
                m = mapped_by_col.get(col)
                if m:
                    writer.writerow(
                        [
                            profile.file_name,
                            profile.sheet_name,
                            domain or "",
                            col,
                            m.standard_field_name,
                            m.business_description,
                            m.field_flag,
                            f"{m.confidence:.2f}",
                            "mapped",
                            m.transform_action,
                            m.transform_note,
                        ]
                    )
                else:
                    writer.writerow(
                        [
                            profile.file_name,
                            profile.sheet_name,
                            domain or "",
                            col,
                            "",
                            "",
                            "",
                            "",
                            "unmapped",
                            "REVIEW",
                            "No confident standard-field match — review / may be market-specific.",
                        ]
                    )


async def build_outputs(state: dict[str, Any]) -> dict[str, Any]:
    """Compute file-level mappings, missing files, and write both CSVs."""
    errors = list(state.get("errors") or [])
    settings = get_settings()
    threshold = settings.confidence_threshold
    market = state.get("market", "Unknown")
    country_key = _market_country_key(market)

    template = TemplateSchema(**(state.get("template") or {}))
    profiles = {
        _profile_key(p["file_name"], p["sheet_name"]): SourceSheetProfile(**p)
        for p in (state.get("source_profiles") or [])
    }
    classifications = [FileClassification(**c) for c in (state.get("classifications") or [])]
    column_mappings = state.get("column_mappings") or {}
    catalog_entries = [SheetCatalogEntry(**e) for e in (state.get("catalog_entries") or [])]

    file_mappings = _build_file_mappings(template, profiles, classifications, country_key)
    missing_files = _build_missing_files(template, classifications, country_key)

    slug = _safe_market_slug(market)
    catalog_path = settings.outputs_dir / f"{slug}_raw_intents_enhanced.csv"
    mapping_path = settings.outputs_dir / f"{slug}_column_mapping.csv"

    catalog_csv_path = ""
    mapping_csv_path = ""
    try:
        _write_catalog_csv(catalog_path, catalog_entries)
        catalog_csv_path = str(catalog_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"build_outputs[catalog_csv]: {exc}")
    try:
        _write_mapping_csv(mapping_path, profiles, classifications, column_mappings, threshold)
        mapping_csv_path = str(mapping_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"build_outputs[mapping_csv]: {exc}")

    return {
        "file_mappings": file_mappings,
        "missing_files": missing_files,
        "catalog_csv_path": catalog_csv_path,
        "mapping_csv_path": mapping_csv_path,
        "errors": errors,
    }
