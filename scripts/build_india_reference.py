"""Build a headers-only India reference skeleton for the nma agent.

The agent was "trained" on India. To give the LLM a concrete target shape when
analysing a NEW market, we bundle a lightweight reference built from the real
India dataset (september_finalized/): every file, every sheet, and the sheet's
column HEADERS only — NO data rows (so nothing confidential ships with the repo).

Output: nma/templates/india_reference/india_sheets.json
  {
    "PMI P&L Database.xlsx": {
        "P&L": ["Variance Description", "Market Group", ...],
        "Mapping": ["Row Labels", ...]
    },
    ...
  }

Run (from nma/):  .venv\\Scripts\\python scripts/build_india_reference.py <india_folder>
If <india_folder> is omitted, defaults to ../september_finalized.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_INDIA = _PROJECT_ROOT.parent / "september_finalized"
_OUT_DIR = _PROJECT_ROOT / "templates" / "india_reference"


def _detect_header(ws, max_scan: int = 15) -> list[str]:
    """Return the first row that looks like a header (>=2 non-empty text cells)."""
    best: list[str] = []
    for row in ws.iter_rows(min_row=1, max_row=max_scan, values_only=True):
        cells = [("" if c is None else str(c).strip()) for c in row]
        non_empty = [c for c in cells if c]
        # A header row tends to have several short, mostly non-numeric labels.
        text_like = [c for c in non_empty if not c.replace(".", "", 1).replace(",", "").isdigit()]
        if len(non_empty) >= 2 and len(text_like) >= max(2, len(non_empty) // 2):
            if len(non_empty) > len(best):
                best = [c for c in cells if c]
    return best


def build(india_folder: Path) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for f in sorted(india_folder.glob("*.xlsx")):
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        sheets: dict[str, list[str]] = {}
        for ws in wb.worksheets:
            header = _detect_header(ws)
            if header:
                sheets[ws.title] = header
        wb.close()
        if sheets:
            result[f.name] = sheets
    return result


def main() -> None:
    india = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_INDIA
    if not india.exists():
        raise SystemExit(f"India folder not found: {india}")

    data = build(india)
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / "india_sheets.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_sheets = sum(len(v) for v in data.values())
    print(f"Wrote {out} — {len(data)} files, {n_sheets} sheets (headers only).")


if __name__ == "__main__":
    main()
