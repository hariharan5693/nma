"""India reference exemplars for the nma agent.

The agent is "trained" on India: we bundle two lightweight India artefacts and
expose them to the LLM nodes as few-shot exemplars when analysing a NEW market.

  1. india_catalog_reference.csv  — the golden enhanced catalog (one row per
     sheet) with columns:
         File_ID, file, sheet, expressed_in, units, actual_columns,
         description, business_facts, granularity_type
     This is the exact shape the agent must reproduce for the new market
     (plus a new `possible_cut` column we add downstream).

  2. india_reference/india_sheets.json — headers-only skeleton
         {file_name: {sheet_name: [column headers...]}}
     (no data rows — nothing confidential ships with the repo).

Both are cached on first load.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

from agent.core.settings import get_settings

# Canonical column order of the enhanced catalog (matches raw_intents_enhanced_v2.csv).
CATALOG_COLUMNS: list[str] = [
    "File_ID",
    "file",
    "sheet",
    "expressed_in",
    "units",
    "actual_columns",
    "description",
    "business_facts",
    "granularity_type",
]

# The extra column the user asked us to add to the NEW-market output.
POSSIBLE_CUT_COLUMN = "possible_cut"

# Allowed vocabulary for possible_cut (multi-valued, free text drawn from these).
POSSIBLE_CUT_VOCAB: list[str] = [
    "Geography",
    "Variant",
    "SKU",
    "Segment",
    "Brand",
    "Channel",
    "Time",
]


def _templates_dir() -> Path:
    return get_settings().template_path.parent


@lru_cache(maxsize=1)
def load_india_catalog() -> list[dict[str, str]]:
    """Load the India golden catalog as a list of row dicts (empty if missing)."""
    path = _templates_dir() / "india_catalog_reference.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


@lru_cache(maxsize=1)
def load_india_sheets() -> dict[str, dict[str, list[str]]]:
    """Load the headers-only India skeleton (empty if missing)."""
    path = _templates_dir() / "india_reference" / "india_sheets.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def india_columns_for_domain(domain_sheet: str) -> list[str]:
    """Best-effort list of the REAL India columns for a domain sheet.

    India files are already live to the downstream agent; this is the reference
    we measure a new market's readiness/parity against (NOT the mapping target —
    the global template is the mapping target). We locate the India catalog row
    whose business meaning matches the domain and return its actual_columns.
    """
    ex = india_catalog_example(domain_sheet, max_chars=100000)
    if not ex:
        return []
    raw = ex.get("actual_columns", "")
    cols = [c.strip() for c in raw.replace("\n", " ").split("|")]
    return [c for c in cols if c and not c.endswith("(truncated)")]


def india_reference_context(domain_sheet: str, max_chars: int = 1200) -> dict[str, str]:
    """Compact India reference block (description + business_facts) for a domain.

    Used ONLY as reference/backup context so the agent understands how a mature,
    live market structures this data. It is NOT the matching target.
    """
    ex = india_catalog_example(domain_sheet, max_chars=max_chars)
    if not ex:
        return {}
    return {
        "india_file": ex.get("file", ""),
        "india_sheet": ex.get("sheet", ""),
        "india_description": ex.get("description", ""),
        "india_business_facts": ex.get("business_facts", ""),
        "india_columns": ex.get("actual_columns", ""),
    }


def india_catalog_example(domain_sheet: str, max_chars: int = 1600) -> dict[str, str] | None:
    """Return the India catalog row whose business meaning best matches a domain sheet.

    Used as a few-shot exemplar when enriching the new market's equivalent sheet.
    Matching is a simple case-insensitive containment on the `file`/`sheet`/File_ID
    against the domain sheet name tokens — good enough to pick a relevant exemplar.
    Long text fields are truncated to keep the prompt compact.
    """
    rows = load_india_catalog()
    if not rows:
        return None

    target = domain_sheet.lower().replace("pmi", "").strip()
    tokens = [t for t in target.replace("&", " ").split() if len(t) > 2]

    best: dict[str, str] | None = None
    best_score = 0
    for row in rows:
        hay = f"{row.get('File_ID','')} {row.get('file','')} {row.get('sheet','')}".lower()
        score = sum(1 for t in tokens if t in hay)
        if score > best_score:
            best_score, best = score, row

    if best is None:
        best = rows[0]

    # Truncate long free-text fields for prompt economy.
    trimmed = dict(best)
    for key in ("description", "business_facts", "actual_columns"):
        val = trimmed.get(key, "")
        if len(val) > max_chars:
            trimmed[key] = val[:max_chars] + " …(truncated)"
    return trimmed
