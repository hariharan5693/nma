"""Run the nma onboarding graph locally over a folder of market files.

Usage (from nma/):
    .venv\\Scripts\\python scripts/run_local.py --market "Thailand" --folder ../Thailand_data

Discovers every .xlsx/.csv in the folder, asks the user for a short one/two line
business description of EACH file (interactive, in the terminal), feeds them to
the graph, runs the full pipeline interactively (the agent may ask clarifying
questions in the terminal when a file match is ambiguous), and prints the final
file-by-file readiness report + output CSV paths.

Flags:
    --market    (required) new market name, e.g. "Thailand"
    --folder    (required) folder of uploaded files
    --no-ask    skip the per-file description prompt (non-interactive run)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make src/ importable when run directly.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.graph import build_graph  # noqa: E402
from agent.services.interactive import (  # noqa: E402
    TerminalPrompter,
    render_clarification,
    set_prompter,
)


def _ask_market(cli_value: str | None, folder: Path) -> str:
    """Ask the user which market this data is for (first step).

    A suggestion is derived from the folder name (e.g. 'Vietnam_Data' -> 'Vietnam').
    Any --market value is offered as the default. The confirmed name labels the
    report and names the output files, so it must match the data.
    """
    suggestion = (cli_value or "").strip()
    if not suggestion:
        stem = folder.name
        for suffix in ("_Data", "_data", "-Data", "-data", " Data", "_extra", "_finalized"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        suggestion = stem.replace("_", " ").replace("-", " ").strip()

    print("\n" + "=" * 80)
    print("STEP 0 — Which market is this data for?")
    print("  (This names the report and the output CSV files, so it must match the data.)")
    print("=" * 80)
    prompt = f"  Market name [{suggestion}] > " if suggestion else "  Market name > "
    while True:
        try:
            resp = input(prompt).strip()
        except EOFError:
            resp = ""
        market = resp or suggestion
        if market:
            return market
        print("  Please enter a market name (e.g. Vietnam).")


def discover(folder: Path) -> list[dict]:
    uploads = []
    for ext in ("*.xlsx", "*.xlsm", "*.xls", "*.csv"):
        for f in sorted(folder.glob(ext)):
            uploads.append({"name": f.name, "path": str(f)})
    return uploads


def ask_descriptions(uploads: list[dict]) -> dict[str, str]:
    """Ask the user for a 1-2 line business description of each uploaded file."""
    print("\n" + "=" * 80)
    print("STEP 1 — Describe each file (one or two lines on what the data represents)")
    print("Tip: say what the data MEANS (e.g. 'distributor-to-retail sell-out volume by")
    print("city and channel') — not just the file name. Press Enter to skip a file.")
    print("=" * 80)
    descriptions: dict[str, str] = {}
    for u in uploads:
        try:
            resp = input(f"\n  [{u['name']}]\n   > ").strip()
        except EOFError:
            resp = ""
        if resp:
            descriptions[u["name"]] = resp
    return descriptions


def _print_report(result: dict) -> None:
    summary = result.get("summary", {})
    print("\n" + "=" * 80)
    print("REPORT")
    print("=" * 80)
    print(summary.get("narrative", "(no narrative)"))
    print()
    print("=" * 80)
    print("ARTEFACTS")
    print("=" * 80)
    print("Verdict:            ", summary.get("readiness_verdict"))
    print("Files analyzed:     ", summary.get("files_analyzed"))
    print("Sheets analyzed:    ", summary.get("sheets_analyzed"))
    print("Domain sheets match:", summary.get("domain_sheets_matched"))
    print("Catalog CSV:        ", summary.get("catalog_csv_path"))
    print("Mapping CSV:        ", summary.get("mapping_csv_path"))
    if result.get("errors"):
        print("\nERRORS:")
        for e in result["errors"]:
            print("  -", e)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=None,
                    help="market name; if omitted you are prompted for it (first step)")
    ap.add_argument("--folder", required=True)
    ap.add_argument("--no-ask", action="store_true", help="skip per-file description prompts")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = (Path(__file__).resolve().parents[1] / folder).resolve()
    if not folder.exists():
        raise SystemExit(f"Folder not found: {folder}")

    # STEP 0 — market name. This labels the whole report AND names the output files
    # (e.g. vietnam_column_mapping.csv), so it MUST match the data being onboarded.
    # Always ask (pre-filling any --market value) so a wrong flag can be corrected.
    market = _ask_market(args.market, folder)

    uploads = discover(folder)
    print(f"\nDiscovered {len(uploads)} files in {folder}:")
    for u in uploads:
        print("  -", u["name"])

    file_descriptions = {} if args.no_ask else ask_descriptions(uploads)

    # Route the agent's clarifying questions to the terminal.
    set_prompter(TerminalPrompter())

    # Build the graph with a checkpointer so interrupt()/resume works.
    graph = build_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": f"local-{market}"}}

    state = {
        "market": market,
        "uploaded_files": uploads,
        "file_descriptions": file_descriptions,
        "messages": [],
    }

    result = await graph.ainvoke(state, config=config)

    # Drain any interrupts (clarifying questions) by asking in the terminal and
    # resuming, until the graph completes.
    while "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        answers = []
        for it in interrupts:
            payload = getattr(it, "value", it)
            if isinstance(payload, dict):
                print(render_clarification(payload))
            else:
                print("\n" + "-" * 80)
                print("AGENT NEEDS CLARIFICATION:")
                print("  " + str(payload))
            ans = input("   your answer > ").strip()
            answers.append(ans)
        resume_value = answers[0] if len(answers) == 1 else answers
        result = await graph.ainvoke(Command(resume=resume_value), config=config)

    _print_report(result)


if __name__ == "__main__":
    asyncio.run(main())
