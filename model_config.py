"""Shared OpenRouter model settings for all agents.

Centralises two cross-cutting concerns so they stay consistent across every
agent and experiment run:

- ``openrouter_reasoning`` — a *fixed* reasoning effort (not ``enabled``).
  A fixed level keeps model-vs-model experiments comparable: every model
  reasons at the same controlled depth instead of each provider picking its
  own default, which can also drift over time. Re-tune all agents at once by
  changing ``REASONING_EFFORT`` here.
- ``openrouter_usage`` — detailed usage/cost accounting in every response, so
  the experiment tracker logs real OpenRouter cost data rather than estimates.
- ``context_window_for`` — each model's real context-window size, used by the
  history-compaction processor (``agent_history.py``) to decide when a run is
  close enough to the limit that older tool outputs should be stubbed. Sourced
  from OpenRouter's public model catalog so it stays correct as models change.
"""

from __future__ import annotations

import asyncio

import httpx
import logfire
from pydantic_ai.models.openrouter import OpenRouterModelSettings, OpenRouterReasoning

# Fixed reasoning effort applied to every agent. One of:
# 'xhigh' | 'high' | 'medium' | 'low' | 'minimal' | 'none'.
REASONING: OpenRouterReasoning = {'effort': 'high'}

# Ask OpenRouter to return detailed usage/cost info on every response.
USAGE_ACCOUNTING = {'include': True}


def agent_model_settings(
    *, parallel_tool_calls: bool = False, timeout: float = 180
) -> OpenRouterModelSettings:
    """Return shared settings: fixed reasoning effort + usage accounting.

    Args:
        parallel_tool_calls: Allow the model to emit tool calls in parallel.
        timeout: Per-request timeout in seconds.
    """
    settings = OpenRouterModelSettings(
        timeout=timeout,
        openrouter_reasoning=REASONING,
        openrouter_usage=USAGE_ACCOUNTING,
    )
    if parallel_tool_calls:
        settings['parallel_tool_calls'] = True
    return settings


# --- Per-model context window resolution -----------------------------------

# Used when the catalog can't be fetched or a model id is missing from it. 200k
# is the smallest window among the models we sweep, so falling back to it makes
# the compaction trigger conservative (fires no later than it should) rather than
# letting an over-large guess delay compaction past the real limit.
DEFAULT_CONTEXT_WINDOW = 200_000

# OpenRouter's public model catalog. No auth required; each entry carries the
# model's `context_length`.
_OPENROUTER_MODELS_URL = 'https://openrouter.ai/api/v1/models'

# Cache: model id -> context_length. Populated once on first need. `None` means
# "not yet fetched"; an empty/partial dict after a failed fetch still serves
# DEFAULT_CONTEXT_WINDOW for every lookup.
_context_windows: dict[str, int] | None = None
_context_windows_lock = asyncio.Lock()


async def _fetch_context_windows() -> dict[str, int]:
    """Fetch the OpenRouter model catalog and map model id -> context_length.

    Returns an empty dict on any failure (network, parse, unexpected shape) — the
    caller falls back to DEFAULT_CONTEXT_WINDOW, so context management degrades to
    a safe conservative window rather than raising inside an experiment run.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get('data', [])
        windows = {
            entry['id']: int(entry['context_length'])
            for entry in data
            if entry.get('id') and entry.get('context_length')
        }
        logfire.info(
            'fetched OpenRouter context windows for {count} models', count=len(windows)
        )
        return windows
    except Exception as exc:  # noqa: BLE001 — never let this break a run
        logfire.warning(
            'could not fetch OpenRouter model catalog ({error}); '
            'using DEFAULT_CONTEXT_WINDOW={default} for all models',
            error=repr(exc),
            default=DEFAULT_CONTEXT_WINDOW,
        )
        return {}


async def context_window_for(model_name: str) -> int:
    """Return the context-window size (tokens) for an OpenRouter model id.

    Fetches the OpenRouter catalog once and caches it process-wide; concurrent
    sub-agents share the single fetch via a lock. Unknown ids and fetch failures
    fall back to DEFAULT_CONTEXT_WINDOW.
    """
    global _context_windows
    if _context_windows is None:
        async with _context_windows_lock:
            if _context_windows is None:  # re-check inside the lock
                _context_windows = await _fetch_context_windows()
    return _context_windows.get(model_name, DEFAULT_CONTEXT_WINDOW)
