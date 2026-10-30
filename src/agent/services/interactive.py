"""Interactive clarification helper for the nma agent.

The agent must NEVER silently assume when a file match is ambiguous — it asks the
user a question and waits for the answer. We support two mechanisms:

  1. LangGraph `interrupt()` (preferred when the graph runs with a checkpointer,
     e.g. LangGraph Studio / deployed / the local runner in scripts/run_local.py).
     The question is surfaced to the host, which resumes with the user's answer.

  2. A direct prompter fallback (set via `set_prompter`) used for plain terminal
     runs where interrupt plumbing is not desired — it blocks on terminal input.

Nodes call `ask_user(question, options=..., context=...)`. It tries interrupt()
first; if that is unavailable (no checkpointer) it falls back to the registered
prompter; if neither is available it returns "" (caller treats as "no answer").
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


def render_clarification(payload: dict) -> str:
    """Render a clarification payload into a clear, file-specific terminal block.

    Shared by the TerminalPrompter and the run_local interrupt loop so BOTH paths
    always show which file/sheet is being asked about — never a vague question.
    """
    lines = ["\n" + "-" * 80, "AGENT NEEDS CLARIFICATION"]
    fname = payload.get("file_name")
    if fname:
        lines.append(f"  File:        {fname}")
    sheet = payload.get("sheet_name")
    if sheet:
        lines.append(f"  Sheet:       {sheet}")
    desc = payload.get("user_description")
    if desc:
        lines.append(f"  Your note:   {desc}")
    cols = payload.get("columns")
    if cols:
        lines.append(f"  Columns:     {cols}")
    best = payload.get("best_guess")
    if best is not None:
        conf = payload.get("confidence")
        conf_txt = f" (confidence {conf})" if conf is not None else ""
        lines.append(f"  Best guess:  {best}{conf_txt}")
    lines.append("")
    lines.append("  " + str(payload.get("question", "")).replace("\n", "\n  "))
    options = payload.get("options")
    if options:
        for i, opt in enumerate(options, 1):
            lines.append(f"    {i}. {opt}")
    return "\n".join(lines)


class Prompter(Protocol):
    def ask(self, payload: dict) -> str: ...


class TerminalPrompter:
    """Blocking terminal prompter for local, non-interrupt runs."""

    def ask(self, payload: dict) -> str:
        print(render_clarification(payload))
        try:
            return input("   your answer > ").strip()
        except EOFError:
            return ""


_PROMPTER: Optional[Prompter] = None


def set_prompter(prompter: Optional[Prompter]) -> None:
    """Register a fallback prompter (used when interrupt() is unavailable)."""
    global _PROMPTER
    _PROMPTER = prompter


def ask_user(
    question: str,
    *,
    options: Optional[list[str]] = None,
    context: str = "",
    **fields: Any,
) -> str:
    """Ask the user a clarifying question; return their answer as a string.

    Extra keyword fields (file_name, sheet_name, user_description, columns,
    best_guess, confidence) are carried in the payload so EVERY render shows
    exactly which file/sheet is being asked about.

    Tries LangGraph interrupt() first (works with a checkpointer); otherwise falls
    back to the registered prompter; otherwise returns "" (no answer available).
    """
    payload: dict[str, Any] = {"question": question}
    if options:
        payload["options"] = options
    if context:
        payload["context"] = context
    for k, v in fields.items():
        if v is not None and v != "":
            payload[k] = v

    # 1) Preferred: LangGraph interrupt (pauses the graph, host resumes with answer).
    try:
        from langgraph.types import interrupt  # local import: optional dependency path

        answer = interrupt(payload)
        if answer is not None:
            return str(answer).strip()
    except Exception:
        # interrupt() raises its own control-flow exception (GraphInterrupt) which
        # we must NOT swallow — re-raise it so the graph actually pauses. Any other
        # error means interrupt is unusable here, so fall through to the prompter.
        import sys

        exc = sys.exc_info()[1]
        if type(exc).__name__ in {"GraphInterrupt", "Interrupt"}:
            raise

    # 2) Fallback: registered prompter (e.g. terminal).
    if _PROMPTER is not None:
        return _PROMPTER.ask(payload)

    # 3) No mechanism available.
    return ""
