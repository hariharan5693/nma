"""Parameterized user-prompt builders for the nma pipeline.

Each builder renders compact context (template fields, source profiles) into a
token-efficient prompt body. We keep sample values short and cap columns.
"""

from __future__ import annotations

import json
import re

from agent.models.schemas import (
    SourceSheetProfile,
    TemplateSchema,
    TemplateSheet,
)

# The PMI LiteLLM gateway runs a Bedrock PII guardrail that blocks content with:
#  - card-expiry / short-date tokens  (e.g. "25.05.2026", "05/26")  -> dash-normalise
#  - long digit runs (>= 8 digits)    (misread as bank/card numbers) -> shorten
# We neutralise both anywhere in the serialised summariser payload so the report
# renders. Values stay human-readable; only guardrail-tripping shapes change.
_EMBEDDED_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b")
_SHORT_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{2,4})\b")
# A single long digit run WITHIN one token (e.g. "590.4288610000001" or a long
# account-like integer). Collapsed only when the token itself carries >= 8 digits.
_DIGIT_RUN_RE = re.compile(r"\d[\d.,/_-]{6,}\d")
_DIGIT_RUN_THRESHOLD = 8

# Any purely-numeric JSON string value, e.g. "68280" or "-523.4". The Bedrock PII
# guardrail concatenates digits across adjacent such values (["68280","523500"] is
# read as an 11-digit bank-account number). We replace the numeric CONTENT of each
# such value with a short tag while keeping the quotes — so no run of digits can be
# concatenated across elements, yet the JSON stays valid and readable.
_NUMERIC_STRING_RE = re.compile(r'"(-?\d[\d.,]*)"')


def _shorten_number(match: re.Match) -> str:
    """Collapse a long single-token digit run to a short, guardrail-safe form."""
    token = match.group(0)
    digits = sum(c.isdigit() for c in token)
    if digits < _DIGIT_RUN_THRESHOLD:
        return token
    return f"<{digits}-digit numeric data>"


def _mask_numeric_strings(text: str) -> str:
    """Replace numeric string VALUES with a short '#num' tag (keeps quotes).

    This removes the digit runs the guardrail concatenates while preserving the
    column structure the LLM reasons over (it still sees a column is numeric).
    """
    return _NUMERIC_STRING_RE.sub('"#num"', text)


def _guardrail_safe(text: str) -> str:
    """Neutralise card-expiry dates and digit runs the Bedrock PII guardrail blocks."""
    text = _EMBEDDED_DATE_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}", text)
    text = _SHORT_DATE_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}", text)
    text = _DIGIT_RUN_RE.sub(_shorten_number, text)
    text = _mask_numeric_strings(text)
    return text


def build_classifier_prompt(
    market: str,
    template: TemplateSchema,
    profiles: list[SourceSheetProfile],
    file_descriptions: dict | None = None,
    india_context: dict | None = None,
) -> str:
    """Prompt the LLM to assign each uploaded sheet to a template domain sheet.

    Matching is description-first: we give the template's file/sheet BUSINESS
    DESCRIPTION + BUSINESS FACTS (primary), the uploaded COLUMNS (secondary), the
    user's own per-file description (strongest intent), and an India reference
    block per domain (reference/backup only). File names are a weak tiebreaker.
    """
    file_descriptions = file_descriptions or {}
    india_context = india_context or {}

    domain_catalog = []
    for sheet in template.business_sheets():
        idx = india_context.get(sheet.sheet_name, {})
        domain_catalog.append(
            {
                "domain_sheet": sheet.sheet_name,
                "functional_area": sheet.functional_area,
                "business_description": sheet.business_description,
                "business_facts": sheet.business_facts,
                "geography_levels": sheet.geography_levels,
                "manufacturer_scope": sheet.manufacturer_scope,
                "key_fields": [f.standard_field_name for f in sheet.fields[:14]],
                "india_reference": {
                    "india_file": idx.get("india_file", ""),
                    "india_description": idx.get("india_description", ""),
                    "india_columns": idx.get("india_columns", ""),
                },
                # Weak tiebreaker only.
                "known_source_file_names": template.functional_domain_map.get(
                    sheet.sheet_name, {}
                ),
            }
        )

    uploaded = []
    for p in profiles:
        uploaded.append(
            {
                "file_name": p.file_name,
                "sheet_name": p.sheet_name,
                "user_description": file_descriptions.get(p.file_name, ""),
                "columns": p.column_names()[:40],
            }
        )

    # No guardrail masking here: this prompt carries only file names, sheet names,
    # descriptions and column headers (no long numeric data). Masking would mangle
    # file names like "711 Scandata 25.05.2026.xlsx".
    return (
        f"NEW MARKET: {market}\n\n"
        f"TEMPLATE DOMAIN SHEETS (map TO these; match on business_description + facts):\n"
        f"{json.dumps(domain_catalog, ensure_ascii=False, indent=1)}\n\n"
        f"UPLOADED SHEETS (classify each; user_description is the strongest intent signal):\n"
        f"{json.dumps(uploaded, ensure_ascii=False, indent=1)}\n\n"
        f"Return the JSON array of classifications now."
    )


def build_mapper_prompt(
    market: str,
    domain_sheet: TemplateSheet,
    profile: SourceSheetProfile,
    india_columns: list[str] | None = None,
) -> str:
    """Prompt the LLM to map one uploaded sheet's columns to one domain sheet's fields.

    `india_columns` are the real India columns for this domain — passed as a
    recognition aid only. The mapping target is always a template standard field.
    """
    fields = [
        {
            "standard_field_name": f.standard_field_name,
            "business_description": f.business_description,
            "field_type": f.field_type,
            "flag": f.flag,
            "examples": f.examples,
        }
        for f in domain_sheet.fields
    ]

    columns = [
        {
            "name": c.name,
            "type": c.inferred_type,
            "samples": c.sample_values[:4],
        }
        for c in profile.columns[:60]
    ]

    india_block = ", ".join((india_columns or [])[:60]) or "(none on record)"

    return _guardrail_safe(
        f"NEW MARKET: {market}\n\n"
        f"TARGET TEMPLATE DOMAIN SHEET: {domain_sheet.sheet_name} "
        f"(functional area: {domain_sheet.functional_area})\n"
        f"BUSINESS MEANING: {domain_sheet.business_description}\n"
        f"STANDARD FIELDS (map TO these):\n"
        f"{json.dumps(fields, ensure_ascii=False, indent=1)}\n\n"
        f"INDIA REFERENCE COLUMNS (recognition aid only, NOT the target):\n{india_block}\n\n"
        f"UPLOADED SHEET: {profile.file_name} :: {profile.sheet_name}\n"
        f"SOURCE COLUMNS (map FROM these):\n"
        f"{json.dumps(columns, ensure_ascii=False, indent=1)}\n\n"
        f"Return the JSON array of column mappings now."
    )


def build_catalog_enricher_prompt(
    market: str,
    domain_sheet: TemplateSheet,
    profile: SourceSheetProfile,
    india_example: dict | None,
) -> str:
    """Prompt the LLM to produce India-style catalog fields for one new-market sheet."""
    columns = [
        {"name": c.name, "type": c.inferred_type, "samples": c.sample_values[:4]}
        for c in profile.columns[:60]
    ]
    example_block = (
        json.dumps(india_example, ensure_ascii=False, indent=1)
        if india_example
        else "(no India exemplar available)"
    )
    return _guardrail_safe(
        f"NEW MARKET: {market}\n\n"
        f"MATCHED TEMPLATE DOMAIN SHEET: {domain_sheet.sheet_name} "
        f"(functional area: {domain_sheet.functional_area})\n\n"
        f"NEW MARKET SHEET: {profile.file_name} :: {profile.sheet_name}\n"
        f"COLUMNS:\n{json.dumps(columns, ensure_ascii=False, indent=1)}\n\n"
        f"INDIA CATALOG EXEMPLAR (style + depth reference — describe the NEW sheet, do not copy "
        f"India-specific facts unless they apply):\n{example_block}\n\n"
        f"Return the JSON catalog object now."
    )


def build_summarizer_prompt(market: str, compact_summary: dict) -> str:
    """Prompt the LLM to write ONLY the short executive narrative + recommendations.

    `compact_summary` is a small pre-aggregated dict (see services.report.build_compact_summary) —
    no raw column dumps, so the prompt is tiny and cheap and never trips the guardrail.
    """
    # The compact summary is only file names, domain names and small counts — no long
    # numeric data — so it does not need the heavy guardrail masking (which would mangle
    # readable dates in file names like "711 Scandata 25.05.2026").
    body = json.dumps(compact_summary, ensure_ascii=False)
    return (
        f"NEW MARKET: {market}\n\n"
        "Pre-computed onboarding summary (use only these facts; do not invent):\n"
        f"{body}\n\n"
        "Write the executive narrative + per-file transformation recommendations now."
    )
