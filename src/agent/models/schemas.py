"""Pydantic models shared across the nma pipeline.

Two groups:
  1. Template schema  — the parsed global template (what we map TO).
  2. Analysis results — profiles, classifications, mappings, gaps, summary.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# 1. Template schema (parsed from templates/global_template.xlsx)
# ─────────────────────────────────────────────────────────────────────────────

FlagType = Literal["M", "C", "O"]  # Mandatory / Conditional / Optional
FieldKind = Literal["KEY", "ATTR", "MEAS", "UNKNOWN"]


class TemplateField(BaseModel):
    """A single standard field within a template domain sheet."""

    standard_field_name: str
    business_description: str = ""
    field_type: FieldKind = "UNKNOWN"
    flag: FlagType = "O"
    # Known per-country source-column hints (used by the LLM to recognise columns).
    examples: dict[str, str] = Field(default_factory=dict)  # {"INDIA": "...", "THAILAND": "..."}


class TemplateSheet(BaseModel):
    """A field-definition sheet in the template (e.g. 'PMI P&L Database' or 'DIM_PRODUCT')."""

    sheet_name: str
    functional_area: str = ""  # Finance / Marketing / Operations / Sales / ...
    is_dimension: bool = False  # True for DIM_* reference tables (not upload targets)
    fields: list[TemplateField] = Field(default_factory=list)
    # File/sheet-level business meaning (from the template's SHEET_DESCRIPTIONS meta
    # sheet). This is the canonical "what this data represents" the classifier matches
    # a user's uploaded file against — NOT the file name.
    business_description: str = ""
    business_facts: str = ""
    geography_levels: str = ""      # e.g. "Region > State/Province > City/Town"
    manufacturer_scope: str = ""    # e.g. "All manufacturers (TMO dimension)."

    @property
    def mandatory_fields(self) -> list[TemplateField]:
        return [f for f in self.fields if f.flag == "M"]


class TemplateSchema(BaseModel):
    """The whole parsed template."""

    version: str = ""
    domain_sheets: list[TemplateSheet] = Field(default_factory=list)
    # Raw file→domain hints from FUNCTIONAL_DOMAIN_MAP: {domain_sheet: {country: filename}}
    functional_domain_map: dict[str, dict[str, str]] = Field(default_factory=dict)

    def sheet_names(self) -> list[str]:
        return [s.sheet_name for s in self.domain_sheets]

    def business_sheets(self) -> list[TemplateSheet]:
        """Domain sheets uploaded files map TO (excludes DIM_* reference tables)."""
        return [s for s in self.domain_sheets if not s.is_dimension]

    def get_sheet(self, name: str) -> Optional[TemplateSheet]:
        for s in self.domain_sheets:
            if s.sheet_name == name:
                return s
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Analysis results
# ─────────────────────────────────────────────────────────────────────────────


class ColumnProfile(BaseModel):
    """A single column discovered in an uploaded source sheet."""

    name: str
    inferred_type: str = "unknown"  # numeric / date / text / mixed
    non_null_count: int = 0
    sample_values: list[str] = Field(default_factory=list)


class SourceSheetProfile(BaseModel):
    """Profile of one sheet within an uploaded file."""

    file_name: str
    sheet_name: str
    row_count: int = 0
    column_count: int = 0
    columns: list[ColumnProfile] = Field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class FileClassification(BaseModel):
    """LLM decision: which template domain sheet an uploaded sheet maps to."""

    file_name: str
    sheet_name: str
    matched_domain_sheet: Optional[str] = None  # None = no confident match
    confidence: float = 0.0
    reasoning: str = ""


TransformAction = Literal["RENAME", "KEEP", "SPLIT", "DERIVE", "UNPIVOT", "DROP", "REVIEW"]


class ColumnMapping(BaseModel):
    """LLM decision: a source column mapped to a standard template field.

    Enriched with the template business description and a concrete transformation
    action so the report can tell the market team exactly how to reshape the data.
    """

    source_column: str
    standard_field_name: str
    confidence: float = 0.0
    reasoning: str = ""
    # Enrichment (populated by the mapper; description back-filled from template).
    business_description: str = ""
    field_flag: FlagType = "O"            # M / C / O of the mapped standard field
    transform_action: TransformAction = "RENAME"
    transform_note: str = ""              # e.g. "rename 'QTYSTICK' -> 'Volume'"


class SheetGapReport(BaseModel):
    """Deterministic gap analysis for one domain sheet."""

    domain_sheet: str
    functional_area: str = ""
    source_file: str = ""
    source_sheet: str = ""
    # Mapped standard fields (confidence >= threshold).
    mapped_fields: list[ColumnMapping] = Field(default_factory=list)
    # Source columns that mapped to nothing.
    unmapped_source_columns: list[str] = Field(default_factory=list)
    # Standard fields with no confident source column, split by flag.
    missing_mandatory: list[str] = Field(default_factory=list)
    missing_conditional: list[str] = Field(default_factory=list)
    missing_optional: list[str] = Field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        total = len(self.mapped_fields) + len(self.missing_mandatory) + len(
            self.missing_conditional
        ) + len(self.missing_optional)
        if total == 0:
            return 0.0
        return round(100.0 * len(self.mapped_fields) / total, 1)


class SheetCatalogEntry(BaseModel):
    """One enriched-catalog row for a matched sheet (mirrors raw_intents_enhanced_v2.csv).

    `possible_cut` is the extra column the onboarding report adds: which analytical
    cuts the sheet can support (Geography / Variant / SKU / Segment / ...).
    """

    file_id: str = ""              # File_ID  (e.g. "P&L_MAIN")
    file: str = ""                 # source file name (uploaded)
    sheet: str = ""                # source sheet name
    expressed_in: str = ""         # expressed_in
    units: str = ""                # units
    actual_columns: str = ""       # pipe-joined source columns
    description: str = ""          # description (LLM, India-style)
    business_facts: str = ""       # business_facts (LLM, India-style)
    granularity_type: str = "Raw"  # granularity_type
    possible_cut: str = ""         # NEW: e.g. "Geography; Variant; SKU"
    # Linkage back to the matched template domain sheet (not written to CSV).
    domain_sheet: str = ""


class FileLevelMapping(BaseModel):
    """File-level mapping: uploaded file -> template/standard file name + rename rec."""

    uploaded_file: str
    matched_domain_sheets: list[str] = Field(default_factory=list)
    functional_areas: list[str] = Field(default_factory=list)
    standard_file_name: str = ""        # template/India/country standard file name
    rename_recommended: bool = False
    rename_recommendation: str = ""     # human-readable action
    sheets_found: list[str] = Field(default_factory=list)


class MissingFile(BaseModel):
    """A template domain sheet that no uploaded file covered."""

    domain_sheet: str
    functional_area: str = ""
    expected_file_name: str = ""        # the country/India standard file name, if known


class FileReadiness(BaseModel):
    """Per-file readiness verdict comparing an uploaded file against BOTH references.

    - vs GLOBAL TEMPLATE  : the mapping target (what the market must conform to).
    - vs INDIA reference   : how a mature, already-live market structures the same
                             domain (reference/backup only, not the target).
    """

    uploaded_file: str
    matched_domain_sheet: str = ""      # template domain this file maps to
    functional_area: str = ""
    match_confidence: float = 0.0
    match_basis: str = ""               # short note: what the match was based on

    # Columns.
    source_column_count: int = 0
    mapped_column_count: int = 0        # confident source cols mapped to a template field

    # Readiness vs GLOBAL TEMPLATE (mandatory-weighted).
    template_total_mandatory: int = 0
    template_mapped_mandatory: int = 0
    template_total_fields: int = 0
    template_mapped_fields: int = 0
    ready_pct_vs_template: float = 0.0          # mandatory coverage % vs template
    ready_pct_vs_template_all: float = 0.0      # all-field coverage % vs template
    missing_mandatory: list[str] = Field(default_factory=list)

    # Parity vs INDIA reference columns.
    india_reference_file: str = ""
    india_reference_column_count: int = 0
    parity_pct_vs_india: float = 0.0            # % of India columns represented

    # Transformations the market team must apply.
    transformations: list[str] = Field(default_factory=list)

    # Explicit references used (so every comparison is traceable).
    compared_against_template: str = ""         # e.g. "Global Template: PMI Secondary Volume"
    compared_against_india: str = ""            # e.g. "India reference: PMI Secondary Volume.xlsx"


class OnboardingSummary(BaseModel):
    """The final structured output of a run."""

    market: str = "Unknown"
    files_analyzed: int = 0
    sheets_analyzed: int = 0
    domain_sheets_matched: int = 0
    total_missing_mandatory: int = 0
    readiness_verdict: str = ""  # e.g. "READY" / "NEEDS ATTENTION" / "NOT READY"
    narrative: str = ""  # business-friendly executive summary (markdown)
    per_sheet: list[SheetGapReport] = Field(default_factory=list)
    file_readiness: list[FileReadiness] = Field(default_factory=list)
    # Output artefacts / enriched views.
    catalog_entries: list[SheetCatalogEntry] = Field(default_factory=list)
    file_mappings: list[FileLevelMapping] = Field(default_factory=list)
    missing_files: list[MissingFile] = Field(default_factory=list)
    catalog_csv_path: str = ""   # written enhanced-catalog CSV
    mapping_csv_path: str = ""   # written column-mapping CSV
