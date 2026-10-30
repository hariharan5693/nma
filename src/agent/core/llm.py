"""LLM factory — a `ChatOpenAI` client pointed at the PMI LiteLLM /v1 gateway.

Follows the pattern validated in test_pre_migration.py: for PMI internal CA
certificates, local runs disable SSL verification via a custom httpx client.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.core.settings import Settings, get_settings


def make_llm(
    settings: Settings | None = None,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> ChatOpenAI:
    """Create a ChatOpenAI client for the LiteLLM gateway.

    Context overrides (model / max_tokens / temperature) win over settings.
    """
    settings = settings or get_settings()
    settings.validate_for_llm()

    verify = settings.verify_ssl
    return ChatOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=model or settings.model,
        max_tokens=max_tokens or settings.max_tokens,
        temperature=settings.temperature if temperature is None else temperature,
        http_client=httpx.Client(verify=verify, timeout=120.0),
        http_async_client=httpx.AsyncClient(verify=verify, timeout=120.0),
    )


def make_fast_llm(
    settings: Settings | None = None,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """ChatOpenAI client using the cheaper 'fast' model (classify / enrich steps)."""
    settings = settings or get_settings()
    return make_llm(settings, model=model or settings.fast_model, max_tokens=max_tokens)


def _extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON object/array from an LLM response.

    Handles ```json fenced blocks and leading/trailing prose.
    """
    cleaned = text.strip()

    # Strip code fences if present.
    if "```" in cleaned:
        start = cleaned.find("```")
        # move past the opening fence line
        newline = cleaned.find("\n", start)
        if newline != -1:
            end = cleaned.find("```", newline)
            if end != -1:
                cleaned = cleaned[newline + 1 : end].strip()

    # Try direct parse first.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to the first {...} or [...] span.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        first = cleaned.find(open_ch)
        last = cleaned.rfind(close_ch)
        if first != -1 and last != -1 and last > first:
            candidate = cleaned[first : last + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # Salvage a TRUNCATED JSON array (response hit max_tokens mid-array):
    # recover every complete top-level {...} object parsed so far.
    salvaged = _salvage_truncated_array(cleaned)
    if salvaged:
        return salvaged

    raise ValueError(f"Could not extract JSON from LLM response:\n{text[:500]}")


def _salvage_truncated_array(text: str) -> list | None:
    """Recover complete objects from a cut-off JSON array like `[ {..}, {..}, {par`."""
    start = text.find("[")
    if start == -1:
        return None
    objects: list = []
    depth = 0
    obj_start = -1
    in_str = False
    escape = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                try:
                    objects.append(json.loads(text[obj_start : i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = -1
    return objects or None


async def ainvoke_json(
    llm: ChatOpenAI,
    system_prompt: str,
    user_prompt: str,
) -> Any:
    """Invoke the LLM and parse the response as JSON (async)."""
    response = await llm.ainvoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    content = response.content if isinstance(response.content, str) else str(response.content)
    return _extract_json(content)


def invoke_json(
    llm: ChatOpenAI,
    system_prompt: str,
    user_prompt: str,
) -> Any:
    """Invoke the LLM and parse the response as JSON (sync)."""
    response = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    content = response.content if isinstance(response.content, str) else str(response.content)
    return _extract_json(content)


async def ainvoke_text(llm: ChatOpenAI, system_prompt: str, user_prompt: str) -> str:
    """Invoke the LLM and return raw text (async)."""
    response = await llm.ainvoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    return response.content if isinstance(response.content, str) else str(response.content)
