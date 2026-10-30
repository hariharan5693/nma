"""Deterministic markdown report builder for the onboarding summary.

The detailed tables (files & sheets, file-level mapping, missing files, downloadable
outputs) are rendered HERE in code — accurate, free, and never tripping the LLM
guardrail. The LLM only writes a short executive narrative + per-file recommendations,
which this module stitches in between the header and the tables.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def build_compact_summary(
    market: str,
    *,
    file_mappings: list[dict],
    gap_reports: list[dict],
    missing_files: list[dict],
    source_profiles: list[dict],
    file_readiness: list[dict] | None = None,
) -> dict[str, Any]:
    """Compact, token-cheap payload for the narrative LLM call (no raw column dumps)."""
    file_readiness = file_readiness or []
    readiness_by_file = {fr.get("uploaded_file", ""): fr for fr in file_readiness}

    # Per-file: matched domains + readiness summary (vs template + vs India).
    files = []
    for fm in file_mappings:
        fname = fm.get("uploaded_file", "")
        fr = readiness_by_file.get(fname, {})
        files.append(
            {
                "file": fname,
                "maps_to": fm.get("matched_domain_sheets", []),
                "areas": fm.get("functional_areas", []),
                "rename": fm.get("rename_recommendation", ""),
                "sheets": len(fm.get("sheets_found", [])),
                "ready_pct_vs_template": fr.get("ready_pct_vs_template", 0.0),
                "parity_pct_vs_india": fr.get("parity_pct_vs_india", 0.0),
                "missing_mandatory": fr.get("missing_mandatory", [])[:6],
                "transformations": fr.get("transformations", [])[:4],
            }
        )

    # Domain coverage (aggregate duplicate domains into one line).
    by_domain: dict[str, dict[str, Any]] = {}
    for r in gap_reports:
        d = r.get("domain_sheet", "")
        agg = by_domain.setdefault(
            d, {"domain": d, "area": r.get("functional_area", ""), "sheets": 0,
                 "mapped": 0, "missing_mandatory": 0}
        )
        agg["sheets"] += 1
        agg["mapped"] += len(r.get("mapped_fields", []))
        agg["missing_mandatory"] += len(r.get("missing_mandatory", []))

    return {
        "market": market,
        "files_analyzed": len({p.get("file_name") for p in source_profiles}),
        "sheets_analyzed": len(source_profiles),
        "files": files,
        "domain_coverage": list(by_domain.values()),
        "missing_domains": [m.get("domain_sheet", "") for m in missing_files],
        "total_missing_mandatory": sum(len(r.get("missing_mandatory", [])) for r in gap_reports),
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return "\n".join(out)


def render_file_readiness(market: str, file_readiness: list[dict]) -> str:
    """Render the file-by-file readiness section.

    For EACH uploaded file: what it matched, how many columns mapped, % ready vs the
    GLOBAL TEMPLATE (the target), parity % vs the INDIA reference, and the concrete
    transformations needed — every comparison citing which reference it used.
    """
    if not file_readiness:
        return ""

    # Headline table.
    rows = []
    for fr in file_readiness:
        matched = fr.get("matched_domain_sheet") or "— (no confident match)"
        rows.append([
            fr.get("uploaded_file", ""),
            matched,
            f"{fr.get('match_confidence', 0.0):.2f}",
            f"{fr.get('mapped_column_count', 0)}/{fr.get('source_column_count', 0)}",
            f"{fr.get('ready_pct_vs_template', 0.0)}%",
            f"{fr.get('parity_pct_vs_india', 0.0)}%",
        ])
    table = _table(
        ["Uploaded File", "Maps to (Global Template)", "Conf.",
         "Cols mapped", "% Ready vs Template", "% Parity vs India"],
        rows,
    )

    # Per-file detail blocks.
    details: list[str] = []
    for fr in file_readiness:
        name = fr.get("uploaded_file", "")
        matched = fr.get("matched_domain_sheet") or "— (no confident match)"
        tmpl_ref = fr.get("compared_against_template", "") or "Global Template"
        india_ref = fr.get("compared_against_india", "") or "India reference (not on record)"
        lines = [
            f"### {name}",
            f"- **Matched domain (target):** {matched}  "
            f"(confidence {fr.get('match_confidence', 0.0):.2f}; basis: {fr.get('match_basis','')})",
            f"- **Readiness vs {tmpl_ref}:** "
            f"{fr.get('ready_pct_vs_template', 0.0)}% of mandatory fields "
            f"({fr.get('template_mapped_mandatory',0)}/{fr.get('template_total_mandatory',0)}), "
            f"{fr.get('ready_pct_vs_template_all', 0.0)}% of all fields "
            f"({fr.get('template_mapped_fields',0)}/{fr.get('template_total_fields',0)}).",
            f"- **Parity vs {india_ref}:** "
            f"{fr.get('parity_pct_vs_india', 0.0)}% of the "
            f"{fr.get('india_reference_column_count',0)} India reference columns represented.",
        ]
        missing = fr.get("missing_mandatory", [])
        if missing:
            lines.append(
                f"- **Missing mandatory (per Global Template):** {', '.join(missing)}"
            )
        transforms = fr.get("transformations", [])
        if transforms:
            lines.append("- **Transformations needed:**")
            for t in transforms[:8]:
                lines.append(f"    - {t}")
        details.append("\n".join(lines))

    return (
        "## File-by-File Readiness\n"
        "_Comparisons cite their reference: the **Global Template** is the mapping "
        "target; the **India reference** shows how a mature, live market structures "
        "the same domain (reference only)._\n\n"
        + table
        + "\n\n"
        + "\n\n".join(details)
    )


def render_tables(
    market: str,
    *,
    file_mappings: list[dict],
    gap_reports: list[dict],
    missing_files: list[dict],
    source_profiles: list[dict],
    catalog_csv_path: str,
    mapping_csv_path: str,
    file_readiness: list[dict] | None = None,
) -> str:
    """Render the deterministic reference tables that follow the narrative."""
    sections: list[str] = []

    # ── File-by-file readiness (the headline section) ──
    readiness_md = render_file_readiness(market, file_readiness or [])
    if readiness_md:
        sections.append(readiness_md)

    # ── Source sheet -> template sheet pairing (grouped, de-duplicated) ──
    # file -> sheet -> matched domain (from gap_reports which carry source_file/source_sheet)
    pair_rows: list[list[str]] = []
    seen = set()
    for r in gap_reports:
        key = (r.get("source_file", ""), r.get("source_sheet", ""))
        if key in seen:
            continue
        seen.add(key)
        # coverage_pct is a computed property (not in model_dump); recompute here.
        mapped = len(r.get("mapped_fields", []))
        total = mapped + len(r.get("missing_mandatory", [])) + len(
            r.get("missing_conditional", [])) + len(r.get("missing_optional", []))
        cov = round(100.0 * mapped / total, 1) if total else 0.0
        pair_rows.append([
            r.get("source_file", ""),
            r.get("source_sheet", ""),
            r.get("domain_sheet", ""),
            r.get("functional_area", ""),
            f"{cov}%",
        ])
    if pair_rows:
        sections.append(
            "## Source Sheet → Global Template Sheet\n"
            + _table(
                ["Uploaded File", "Sheet", "Template Sheet", "Area", "Coverage"],
                pair_rows,
            )
        )

    # ── File-level mapping + rename recommendation ──
    fm_rows = []
    for fm in file_mappings:
        maps = ", ".join(fm.get("matched_domain_sheets", [])) or "— (no confident match)"
        areas = ", ".join(fm.get("functional_areas", [])) or "—"
        fm_rows.append([
            fm.get("uploaded_file", ""), maps, areas, fm.get("rename_recommendation", ""),
        ])
    sections.append(
        "## File-Level Mapping & Rename Recommendation\n"
        + _table(["Uploaded File", "Maps to (Template)", "Area", "Rename Recommendation"], fm_rows)
    )

    # ── Missing domains ──
    if missing_files:
        mf_rows = [
            [m.get("domain_sheet", ""), m.get("functional_area", "") or "—",
             m.get("expected_file_name", "") or "—"]
            for m in missing_files
        ]
        sections.append(
            "## Missing Files / Domains (per Global Template)\n"
            + _table(["Expected Domain", "Area", "Expected File"], mf_rows)
        )

    # ── Downloadable outputs ──
    sections.append(
        "## Downloadable Outputs\n"
        f"- Enhanced catalog CSV: `{catalog_csv_path or 'not generated'}`\n"
        f"- Column mapping CSV: `{mapping_csv_path or 'not generated'}`"
    )

    return "\n\n".join(sections)
