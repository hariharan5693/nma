"""LangGraph entry point for the New Market Onboarding (nma) agent.

Compiled `graph` object referenced by langgraph.json (`./src/agent/graph.py:graph`,
assistant id "agent"). Linear onboarding pipeline:

    START
      → load_template     parse the bundled global template (+ SHEET_DESCRIPTIONS)
      → ingest_files      resolve inputs + profile uploaded sheets
      → classify_files    [LLM + INTERACTIVE] match sheet → template domain using the
                          user's per-file description + columns + template business
                          description; ASK the user when ambiguous (never assume)
      → map_columns       [LLM] source columns → standard fields + transformation
                          (India columns passed as recognition aid only)
      → gap_analysis      [code] matched / unmapped / missing (M/C/O)
      → file_readiness    [code] per-file % ready vs GLOBAL TEMPLATE + parity vs INDIA
      → enrich_catalog    [LLM] India-style catalog row per sheet (+ possible_cut)
      → build_outputs     [code] file-level mapping, missing files, write 2 CSVs
      → summarize         [LLM] professional, file-by-file onboarding report
      → END

State is MessagesState-based, so memory works locally (in-memory checkpointer)
and automatically with PostgreSQL once deployed (A2A stateful registration).
"""

from __future__ import annotations

from typing import Optional

from langgraph.graph import START, StateGraph
from typing_extensions import TypedDict

from agent.core.bootstrap import apply_local_ssl_workaround
from agent.nodes.build_outputs import build_outputs
from agent.nodes.classify_files import classify_files
from agent.nodes.enrich_catalog import enrich_catalog
from agent.nodes.file_readiness import file_readiness
from agent.nodes.gap_analysis import gap_analysis
from agent.nodes.ingest_files import ingest_files
from agent.nodes.load_template import load_template
from agent.nodes.map_columns import map_columns
from agent.nodes.summarize import summarize
from agent.state import OnboardingState

# Apply the local SSL workaround for PMI internal certs (no-op when deployed).
apply_local_ssl_workaround()


class Context(TypedDict, total=False):
    """Per-assistant / per-invocation configuration.

    Set when creating assistants or invoking the graph. All optional — nodes
    fall back to environment-driven settings when a key is absent.
    """

    model: str
    max_tokens: int
    api_key: str
    base_url: str


def _builder() -> StateGraph:
    """Assemble the onboarding StateGraph (uncompiled)."""
    return (
        StateGraph(OnboardingState, context_schema=Context)
        .add_node("load_template", load_template)
        .add_node("ingest_files", ingest_files)
        .add_node("classify_files", classify_files)
        .add_node("map_columns", map_columns)
        .add_node("gap_analysis", gap_analysis)
        .add_node("file_readiness", file_readiness)
        .add_node("enrich_catalog", enrich_catalog)
        .add_node("build_outputs", build_outputs)
        .add_node("summarize", summarize)
        .add_edge(START, "load_template")
        .add_edge("load_template", "ingest_files")
        .add_edge("ingest_files", "classify_files")
        .add_edge("classify_files", "map_columns")
        .add_edge("map_columns", "gap_analysis")
        .add_edge("gap_analysis", "file_readiness")
        .add_edge("file_readiness", "enrich_catalog")
        .add_edge("enrich_catalog", "build_outputs")
        .add_edge("build_outputs", "summarize")
    )


def build_graph(checkpointer=None):
    """Compile the graph. Pass a checkpointer to enable interrupt()/resume.

    The interactive clarification step (classify_files) uses LangGraph interrupt()
    which REQUIRES a checkpointer to pause and resume. The local runner and the
    deployed platform both provide one; LangGraph Studio provides its own.
    """
    return _builder().compile(name="New Market Onboarding Agent", checkpointer=checkpointer)


# Default compiled graph referenced by langgraph.json. The platform / Studio
# inject their own checkpointer, so no explicit one is needed here.
graph = build_graph()
