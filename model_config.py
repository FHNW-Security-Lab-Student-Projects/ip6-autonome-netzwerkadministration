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
from dataclasses import dataclass

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
# model's `context_length` and a `pricing` block (per-token USD prices).
_OPENROUTER_MODELS_URL = 'https://openrouter.ai/api/v1/models'


@dataclass(frozen=True)
class ModelPricing:
    """OpenRouter list price for a model, normalised to USD per million tokens.

    OpenRouter publishes `pricing.*` as USD *per token* (or per request); we
    multiply the per-token figures by 1e6 so they read naturally ("$/Mtok").

    This is the model's *advertised* (default/cheapest-provider) rate. We use it
    to compute a modeled cost — `tokens × rate` — that is independent of which
    provider OpenRouter happened to route to and of any prompt-cache discount.
    """
    prompt_per_mtok: float
    completion_per_mtok: float
    # Separate price for internal reasoning tokens, if the model lists one. When
    # None, reasoning tokens are billed at the completion rate (and are already
    # included in the completion token count).
    reasoning_per_mtok: float | None = None
    # Flat per-request fee, if any (most models have none).
    request_usd: float = 0.0

    def cost_for(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
        requests: int = 0,
    ) -> float:
        """Modeled cost in USD for the given native token counts.

        `completion_tokens` is the full native completion count, which includes
        reasoning tokens. When the model prices reasoning separately, the
        reasoning portion is split out and charged at `reasoning_per_mtok`;
        otherwise the whole completion count is charged at the completion rate.
        """
        cost = prompt_tokens * self.prompt_per_mtok / 1_000_000
        if self.reasoning_per_mtok is not None and reasoning_tokens:
            visible = max(completion_tokens - reasoning_tokens, 0)
            cost += visible * self.completion_per_mtok / 1_000_000
            cost += reasoning_tokens * self.reasoning_per_mtok / 1_000_000
        else:
            cost += completion_tokens * self.completion_per_mtok / 1_000_000
        cost += requests * self.request_usd
        return cost


# Cache: model id -> catalog entry. Populated once on first need. `None` means
# "not yet fetched"; an empty/partial dict after a failed fetch still serves the
# safe fallbacks for every lookup.
_catalog: dict[str, dict] | None = None
_catalog_lock = asyncio.Lock()


async def _fetch_catalog() -> dict[str, dict]:
    """Fetch the OpenRouter model catalog and map model id -> catalog entry.

    Returns an empty dict on any failure (network, parse, unexpected shape) — the
    callers fall back to safe defaults, so context management and pricing display
    degrade gracefully rather than raising inside an experiment run.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get('data', [])
        catalog = {entry['id']: entry for entry in data if entry.get('id')}
        logfire.info(
            'fetched OpenRouter model catalog for {count} models', count=len(catalog)
        )
        return catalog
    except Exception as exc:  # noqa: BLE001 — never let this break a run
        logfire.warning(
            'could not fetch OpenRouter model catalog ({error}); '
            'using DEFAULT_CONTEXT_WINDOW={default} and no pricing for all models',
            error=repr(exc),
            default=DEFAULT_CONTEXT_WINDOW,
        )
        return {}


async def _get_catalog() -> dict[str, dict]:
    """Return the process-wide catalog cache, fetching once under a lock."""
    global _catalog
    if _catalog is None:
        async with _catalog_lock:
            if _catalog is None:  # re-check inside the lock
                _catalog = await _fetch_catalog()
    return _catalog


async def context_window_for(model_name: str) -> int:
    """Return the context-window size (tokens) for an OpenRouter model id.

    Fetches the OpenRouter catalog once and caches it process-wide; concurrent
    sub-agents share the single fetch via a lock. Unknown ids and fetch failures
    fall back to DEFAULT_CONTEXT_WINDOW.
    """
    entry = (await _get_catalog()).get(model_name)
    if entry and entry.get('context_length'):
        return int(entry['context_length'])
    return DEFAULT_CONTEXT_WINDOW


async def pricing_for(model_name: str) -> ModelPricing | None:
    """Return the OpenRouter list price (USD per million tokens) for a model id.

    Shares the cached catalog fetch with context_window_for. Returns None when the
    catalog couldn't be fetched, the id is unknown, or the entry has no usable
    pricing — callers should treat that as "pricing unavailable".
    """
    entry = (await _get_catalog()).get(model_name)
    pricing = entry.get('pricing') if entry else None
    if not pricing:
        return None
    try:
        prompt = float(pricing.get('prompt') or 0.0)
        completion = float(pricing.get('completion') or 0.0)
        reasoning = float(pricing.get('internal_reasoning') or 0.0)
        request = float(pricing.get('request') or 0.0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0.0 and completion <= 0.0:
        return None
    return ModelPricing(
        prompt_per_mtok=prompt * 1_000_000,
        completion_per_mtok=completion * 1_000_000,
        reasoning_per_mtok=(reasoning * 1_000_000) if reasoning > 0.0 else None,
        request_usd=request,
    )
