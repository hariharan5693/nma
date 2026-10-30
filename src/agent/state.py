"""Graph state for the nma onboarding pipeline.

Based on `MessagesState` so that:
  - conversation memory works locally (in-memory checkpointer) and with
    PostgreSQL when deployed, and
  - the agent can be registered in LiteLLM with protocol="a2a" for stateful,
    multi-turn follow-up questions about the mapping.

The pipeline-specific fields are plain keys alongside the inherited `messages`.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import MessagesState


class OnboardingState(MessagesState):
    """State carried through the onboarding graph.

    Inherited:
        messages: list[BaseMessage]   # conversation history (MessagesState)

    Pipeline fields (all optional until the owning node populates them):
    """

    # ── Inputs ──
    market: str                       # new market name, e.g. "Philippines"
    uploaded_files: list[dict[str, Any]]   # [{"name": str, "path": str}] OR {"name","content_b64"}
    # User-supplied 1-2 line business description PER uploaded file: {file_name: text}.
    # This is the PRIMARY signal the classifier uses to match a file to a template
    # domain (never the file name alone).
    file_descriptions: dict[str, str]

    # ── Intermediate artifacts (serialisable dicts for checkpointing) ──
    template: dict[str, Any]          # TemplateSchema.model_dump()
    source_profiles: list[dict[str, Any]]      # [SourceSheetProfile.model_dump()]
    classifications: list[dict[str, Any]]      # [FileClassification.model_dump()]
    column_mappings: dict[str, list[dict[str, Any]]]  # key "file::sheet" -> [ColumnMapping]
    gap_reports: list[dict[str, Any]]          # [SheetGapReport.model_dump()]
    file_readiness: list[dict[str, Any]]       # [FileReadiness.model_dump()] per-file % ready
    catalog_entries: list[dict[str, Any]]      # [SheetCatalogEntry.model_dump()]
    file_mappings: list[dict[str, Any]]        # [FileLevelMapping.model_dump()]
    missing_files: list[dict[str, Any]]        # [MissingFile.model_dump()]
    catalog_csv_path: str                      # path to the written enhanced-catalog CSV
    mapping_csv_path: str                      # path to the written column-mapping CSV

    # ── Output ──
    summary: dict[str, Any]           # OnboardingSummary.model_dump()

    # ── Diagnostics ──
    errors: list[str]
