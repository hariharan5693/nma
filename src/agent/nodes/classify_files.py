"""Node: classify_files — [LLM] assign each uploaded sheet to a template domain sheet.

Description-first matching: the user's per-file description + the uploaded columns +
the template's file/sheet BUSINESS DESCRIPTION are the signals. India catalog context
is passed as reference/backup only. File names are a weak tiebreaker.

Interactive: when a match is ambiguous or low-confidence, the agent ASKS the user a
clarifying question (via interrupt() or the terminal prompter) and never assumes.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agent.core.llm import ainvoke_json, make_fast_llm
from agent.core.settings import get_settings
from agent.models.schemas import FileClassification, SourceSheetProfile, TemplateSchema
from agent.prompts.system_prompts import CLASSIFIER_SYSTEM
from agent.prompts.user_prompts import build_classifier_prompt
from agent.services.interactive import ask_user
from agent.services.reference import india_reference_context

# Below this confidence a match is treated as "needs clarification".
_CLARIFY_BELOW = 0.70


def _india_context_for(template: TemplateSchema) -> dict[str, dict[str, str]]:
    """Build {domain_sheet: india reference block} for the business sheets."""
    ctx: dict[str, dict[str, str]] = {}
    for sheet in template.business_sheets():
        block = india_reference_context(sheet.sheet_name)
        if block:
            ctx[sheet.sheet_name] = block
    return ctx


async def classify_files(state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    """Classify every uploaded sheet into a template domain sheet (or none)."""
    errors = list(state.get("errors") or [])
    profiles = [SourceSheetProfile(**p) for p in (state.get("source_profiles") or [])]
    template = TemplateSchema(**(state.get("template") or {}))
    file_descriptions = dict(state.get("file_descriptions") or {})

    if not profiles or not template.business_sheets():
        errors.append("classify_files: missing profiles or template; skipping.")
        return {"classifications": [], "errors": errors}

    ctx = getattr(runtime, "context", None) or {}
    llm = make_fast_llm(model=ctx.get("model"))

    market = state.get("market", "Unknown")
    india_ctx = _india_context_for(template)
    prompt = build_classifier_prompt(
        market, template, profiles, file_descriptions, india_ctx
    )

    valid_sheets = {s.sheet_name for s in template.business_sheets()}
    raw_items: list[dict[str, Any]] = []
    try:
        raw = await ainvoke_json(llm, CLASSIFIER_SYSTEM, prompt)
        raw_items = raw if isinstance(raw, list) else []
    except Exception as exc:  # noqa: BLE001
        errors.append(f"classify_files: {exc}")

    # Build a lookup of profile by (file, sheet) for context in clarifications.
    prof_by_key = {(p.file_name, p.sheet_name): p for p in profiles}

    classifications: list[dict[str, Any]] = []
    for item in raw_items:
        file_name = item.get("file_name", "")
        sheet_name = item.get("sheet_name", "")
        matched = item.get("matched_domain_sheet")
        if matched and matched not in valid_sheets:
            matched = None  # LLM hallucinated a sheet name
        confidence = float(item.get("confidence", 0.0) or 0.0)
        reasoning = item.get("reasoning", "")
        clarify_q = item.get("clarify_question", "") or ""

        # Interactive clarification: ambiguous (no match) or low confidence.
        needs_clarify = matched is None or confidence < _CLARIFY_BELOW
        if needs_clarify:
            prof = prof_by_key.get((file_name, sheet_name))
            cols = ", ".join(prof.column_names()[:12]) if prof else "(could not read columns)"
            user_desc = file_descriptions.get(file_name, "") or "(none given)"
            best = matched or "none"

            # Always lead with the EXACT file + sheet so the user knows what is
            # being asked about. The LLM's clarify_question (if any) is appended
            # as extra detail, never used on its own.
            question = (
                f"FILE: {file_name}  (sheet: {sheet_name})\n"
                f"   Which global-template domain does this file represent? "
                f"Reply with a number (1-{len(valid_sheets)}) or the domain name."
            )
            if clarify_q:
                question += f"\n   (detail: {clarify_q})"

            options = sorted(valid_sheets)
            answer = ask_user(
                question,
                options=options,
                context="",
                file_name=file_name,
                sheet_name=sheet_name,
                user_description=user_desc,
                columns=cols,
                best_guess=best,
                confidence=round(confidence, 2),
            )
            resolved = _resolve_answer(answer, valid_sheets)
            if resolved:
                matched = resolved
                confidence = max(confidence, 0.95)  # user-confirmed
                reasoning = f"User-confirmed: {resolved}. ({reasoning})".strip()
            elif answer.strip().lower() in {"", "none", "skip", "no", "n/a"}:
                matched = None  # user says no confident domain

        classifications.append(
            FileClassification(
                file_name=file_name,
                sheet_name=sheet_name,
                matched_domain_sheet=matched,
                confidence=confidence,
                reasoning=reasoning,
            ).model_dump()
        )

    return {"classifications": classifications, "errors": errors}


def _resolve_answer(answer: str, valid_sheets: set[str]) -> str | None:
    """Resolve a user's free-text/number answer to a valid domain sheet name."""
    a = (answer or "").strip()
    if not a:
        return None
    options = sorted(valid_sheets)
    # Numeric choice (1-based index into the presented options).
    if a.isdigit():
        i = int(a) - 1
        if 0 <= i < len(options):
            return options[i]
        return None
    # Exact (case-insensitive) match.
    low = a.lower()
    for s in options:
        if s.lower() == low:
            return s
    # Substring / contains match (lenient).
    for s in options:
        if low in s.lower() or s.lower() in low:
            return s
    return None
