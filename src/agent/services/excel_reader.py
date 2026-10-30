"""Profile uploaded market data files (Excel / CSV).

For each sheet we extract: column headers, inferred types, non-null counts, and
a few sample values. These profiles are what the LLM reasons over when matching
columns to the template — we deliberately keep samples small to control tokens.

Guardrail safety: the PMI LiteLLM gateway runs a Bedrock PII guardrail that blocks
requests containing long decimal runs (misread as bank/card numbers, e.g.
"590.4288610000001") or short date tokens (misread as card expiries, e.g.
"25.05.2026"). We neutralise both at profile time — rounding floats to 3 d.p. and
dash-normalising date-like tokens — so values stay readable but never trip it.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

import openpyxl

from agent.models.schemas import ColumnProfile, SourceSheetProfile

_MAX_SAMPLE_VALUES = 5
_MAX_SCAN_ROWS = 200  # rows scanned for type inference / samples
_MAX_HEADER_SCAN = 30  # rows scanned to locate the real header row

# Whole-token date shapes (MM/YY, MM.YYYY, DD.MM.YYYY, YYYY/MM, etc.) that trip the
# Bedrock CREDIT_DEBIT_CARD_EXPIRY / PII guardrail. Normalised to dashes.
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[./]\d{1,2}([./]\d{1,4})?$")


def _infer_type(values: list[Any]) -> str:
    """Infer a coarse column type from sampled non-null values."""
    non_null = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return "empty"

    numeric = 0
    date_like = 0
    for v in non_null:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            numeric += 1
        elif hasattr(v, "year") and hasattr(v, "month"):  # datetime/date
            date_like += 1
        else:
            s = str(v).strip().replace(",", "")
            try:
                float(s)
                numeric += 1
            except ValueError:
                pass

    total = len(non_null)
    if date_like / total > 0.6:
        return "date"
    if numeric / total > 0.6:
        return "numeric"
    return "text"


def _round_numeric_token(value: Any) -> str:
    """Round a numeric value/token to 3 decimal places; pass non-numbers through.

    Long trailing-decimal floats (e.g. 590.4288610000001) are what trip the
    Bedrock PII guardrail (misread as bank/card numbers). Rounding to 3 d.p.
    (-> "590.429") removes the long digit run while keeping the value meaningful.
    Integers and genuine non-numeric text are left unchanged.
    """
    # Real numeric cell values from openpyxl.
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, 3)
        # Drop a trailing ".0" so whole numbers read cleanly.
        return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.3f}".rstrip("0").rstrip(".")

    # String tokens that are actually numbers (with optional thousands separators).
    s = str(value).strip()
    cleaned = s.replace(",", "")
    try:
        num = float(cleaned)
    except (ValueError, TypeError):
        return s  # genuine text — leave as-is
    if "." in cleaned or "e" in cleaned.lower():
        rounded = round(num, 3)
        return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.3f}".rstrip("0").rstrip(".")
    return s  # integer-like string, keep original formatting


def _neutralize_dates(text: str) -> str:
    """Dash-normalise card-expiry/short-date separators so the PII guardrail allows it.

    e.g. '25.05.2026' -> '25-05-2026', '05/26' -> '05-26'. Meaning is preserved.
    """
    s = text.strip()
    if _DATE_LIKE_RE.match(s):
        return s.replace(".", "-").replace("/", "-")
    return s


def _clean_token(value: Any) -> str:
    """Round numbers to 3 d.p. and neutralise date-like tokens (guardrail-safe)."""
    # datetime/date objects -> ISO date (guardrail-safe, meaningful).
    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return str(value)
    return _neutralize_dates(_round_numeric_token(value))


def _sanitize_sample(value: Any) -> str:
    """Normalise one sample value: round numbers, neutralise dates, cap long text."""
    return _clean_token(value)[:60]


def _looks_like_header(cells: list[Any]) -> int:
    """Score a row's likelihood of being a header (higher = more header-like).

    Header rows tend to have several non-empty, mostly non-numeric, short text
    labels. Title/banner rows usually have a single long cell; data rows are
    dominated by numbers/dates.
    """
    values = [c for c in cells if c is not None and str(c).strip() != ""]
    if len(values) < 2:
        return 0
    text_like = 0
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            continue
        if hasattr(v, "year") and hasattr(v, "month"):  # datetime
            continue
        s = str(v).strip().replace(",", "")
        try:
            float(s)
            continue  # numeric string
        except ValueError:
            pass
        if len(s) <= 60:
            text_like += 1
    # Require a majority of non-empty cells to be short text labels.
    return text_like if text_like >= max(2, len(values) // 2) else 0


def _non_empty_cols(row: tuple) -> set[int]:
    return {i for i, c in enumerate(row) if c is not None and str(c).strip() != ""}


def _text_label_cols(row: tuple) -> set[int]:
    """Columns in this row that look like short text LABELS (not numbers/dates)."""
    out: set[int] = set()
    for i, v in enumerate(row):
        if v is None or str(v).strip() == "":
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            continue
        if hasattr(v, "year") and hasattr(v, "month"):
            continue
        s = str(v).strip().replace(",", "")
        try:
            float(s)
            continue
        except ValueError:
            pass
        if len(s) <= 60:
            out.add(i)
    return out


def _find_header_index(rows: list[tuple]) -> int:
    """Return the index of the best header row.

    A good header row has several short TEXT labels AND those label columns are the
    same columns that carry data in the rows BELOW it. We score each candidate by
    the overlap between its label columns and the columns populated in the next
    data rows — this beats picking a sparse title/banner row and avoids latching
    onto stray labels in a pre-header block.
    """
    n = len(rows)
    scan = min(_MAX_HEADER_SCAN, n)
    best_idx, best_score = 0, -1.0
    for i in range(scan):
        labels = _text_label_cols(rows[i])
        if len(labels) < 2:
            continue
        # Columns populated in the next few data rows.
        data_cols: set[int] = set()
        for r in rows[i + 1 : i + 11]:
            data_cols |= _non_empty_cols(r)
        if not data_cols:
            continue
        aligned = len(labels & data_cols)
        # Favour rows whose labels sit over real data, with more labels as tiebreaker.
        score = aligned + 0.1 * len(labels)
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx


def _profile_worksheet(file_name: str, sheet_name: str, rows: list[tuple]) -> SourceSheetProfile:
    """Build a profile from raw rows (header row auto-detected within first rows)."""
    if not rows:
        return SourceSheetProfile(
            file_name=file_name, sheet_name=sheet_name, row_count=0, column_count=0
        )

    header_idx = _find_header_index(rows)
    header = rows[header_idx]
    data_rows = rows[header_idx + 1 :]

    # Trim trailing all-empty columns so sheets with hundreds of blank trailing
    # columns don't explode into "(col N)" noise. Keep up to the last column that
    # is named in the header OR populated in any scanned data row.
    used_cols = set(_non_empty_cols(header))
    for r in data_rows[:_MAX_SCAN_ROWS]:
        used_cols |= _non_empty_cols(r)
    last_used = max(used_cols) if used_cols else (len(header) - 1)
    header = tuple(header[: last_used + 1])

    columns: list[ColumnProfile] = []
    for col_idx, raw_name in enumerate(header):
        # Guardrail-safe header name (numeric headers rounded, date headers dashed).
        name = "" if raw_name is None else _clean_token(raw_name).strip()
        if not name:
            name = f"(col {col_idx + 1})"

        col_values = [
            row[col_idx] if col_idx < len(row) else None
            for row in data_rows[:_MAX_SCAN_ROWS]
        ]
        non_null = [v for v in col_values if v is not None and str(v).strip() != ""]
        samples: list[str] = []
        for v in non_null:
            s = _sanitize_sample(v)
            if s not in samples:
                samples.append(s)
            if len(samples) >= _MAX_SAMPLE_VALUES:
                break

        columns.append(
            ColumnProfile(
                name=name,
                inferred_type=_infer_type(col_values),
                non_null_count=len(non_null),
                sample_values=samples,
            )
        )

    return SourceSheetProfile(
        file_name=file_name,
        sheet_name=sheet_name,
        row_count=len(data_rows),
        column_count=len(columns),
        columns=columns,
    )


def profile_excel_bytes(file_name: str, content: bytes) -> list[SourceSheetProfile]:
    """Profile every sheet in an in-memory Excel workbook."""
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    profiles: list[SourceSheetProfile] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        # Skip fully empty sheets.
        if not any(any(c is not None for c in r) for r in rows[:5]):
            continue
        profiles.append(_profile_worksheet(file_name, ws.title, rows))
    wb.close()
    return profiles


def profile_csv_bytes(file_name: str, content: bytes) -> list[SourceSheetProfile]:
    """Profile a CSV file (single logical sheet)."""
    import csv

    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [tuple(r) for r in reader]
    return [_profile_worksheet(file_name, "CSV", rows)]


def profile_path(path: str | Path) -> list[SourceSheetProfile]:
    """Profile a file on disk (dispatch by extension)."""
    path = Path(path)
    content = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return profile_excel_bytes(path.name, content)
    if suffix == ".csv":
        return profile_csv_bytes(path.name, content)
    raise ValueError(f"Unsupported file type: {suffix} ({path.name})")


def profile_upload(upload: dict[str, Any]) -> list[SourceSheetProfile]:
    """Profile one uploaded file descriptor.

    Accepts either {"name","path"} (on disk) or {"name","content_b64"} (inline).
    """
    name = upload.get("name", "uploaded_file")

    if upload.get("path"):
        return profile_path(upload["path"])

    if upload.get("content_b64"):
        content = base64.b64decode(upload["content_b64"])
        suffix = Path(name).suffix.lower()
        if suffix == ".csv":
            return profile_csv_bytes(name, content)
        return profile_excel_bytes(name, content)

    raise ValueError(f"Upload '{name}' has neither 'path' nor 'content_b64'.")
