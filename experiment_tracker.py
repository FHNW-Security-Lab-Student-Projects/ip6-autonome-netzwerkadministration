"""Experiment tracker for LLM comparison research.

Records per-session metrics (tokens, cost, latency, tool calls) for each user turn.
Supports structured comparison of different LLMs on network troubleshooting tasks.

Outputs:
  - experiment_log.jsonl  — one JSON record per session, append-only, for offline analysis
  - Logfire span          — emitted per session so metrics are queryable via SQL dashboard

Typical usage:
    session = begin_session(user_query="...", model="z-ai/glm-5", scenario="bgp-flap")
    with capture_generation_ids() as gen_ids:
        result = await some_agent.run(request)
    await record_agent_run(session, "network_agent", "z-ai/glm-5", result, gen_ids)
    finish_session(session, success=True)

Tokens come from two sources. NATIVE counts (provider tokenizer) come from
result.usage() and are captured at run time, so they're always complete — this is
what OpenRouter bills on and the basis for cost. NORMALIZED counts (model-agnostic
GPT tokenizer) come from the OpenRouter Generation API and are used to compare
"effort" across models; they can be invalid (None) when a generation lags behind
OpenRouter's indexing. Duration also comes from the Generation API (generation_time).
Cost is *modeled*: native_tokens × the model's advertised per-token rate (from the
OpenRouter model catalog — see model_config.pricing_for). This makes cost
independent of which provider OpenRouter routed to and of any prompt-cache
discount, so it's reproducible and comparable across models. It is therefore a
list-price estimate, not the dollar amount actually billed — see finalize_costs.
duration_s is summed from generation_time across all LLM round-trips for the run.
"""

import asyncio
import contextlib
import json
import os
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import httpx
import logfire
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelResponse, ToolCallPart

from model_config import ModelPricing, pricing_for

EXPERIMENT_LOG = Path(__file__).parent / 'experiment_log.jsonl'

# --- OpenRouter generation ID tracking ---

_OPENROUTER_GENERATION_URL = 'https://openrouter.ai/api/v1/generation'
_capturing_ids: ContextVar[list[str] | None] = ContextVar('_capturing_ids', default=None)


async def _on_openrouter_response(response: httpx.Response) -> None:
    """httpx response hook — appends X-Generation-Id to the active capture list."""
    gen_id = response.headers.get('x-generation-id')
    if gen_id:
        ids = _capturing_ids.get()
        if ids is not None:
            ids.append(gen_id)


def make_tracked_http_client() -> httpx.AsyncClient:
    """Return an httpx.AsyncClient that captures OpenRouter generation IDs via response hook.

    Timeout matches Pydantic AI's default (600s read, 5s connect) so LLM inference
    calls don't time out prematurely.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout=600, connect=5),
        event_hooks={'response': [_on_openrouter_response]},
    )


def _gen_id_sort_key(gen_id: str) -> tuple[int, object]:
    """Sort key giving chronological order for OpenRouter generation IDs.

    IDs look like ``gen-<unix_ms>-<rand>``; sort by the embedded timestamp when present.
    Anything that doesn't parse falls back to lexicographic order and sorts after the
    timestamped ones (the tuple's first element keeps the two groups from comparing
    int against str).
    """
    parts = gen_id.split('-')
    if len(parts) >= 2 and parts[1].isdigit():
        return (0, int(parts[1]))
    return (1, gen_id)


@contextlib.contextmanager
def capture_generation_ids() -> Generator[list[str], None, None]:
    """Sync context manager — collects OpenRouter generation IDs during an agent run.

    Usage:
        with capture_generation_ids() as gen_ids:
            result = await agent.run(...)
        # later, pass gen_ids to finalize_costs() to attach token/cost data.
    """
    ids: list[str] = []
    token = _capturing_ids.set(ids)
    try:
        yield ids
    finally:
        _capturing_ids.reset(token)


def _count_tool_calls(result: AgentRunResult) -> int:
    """Count MCP/tool calls made across all messages in an agent run."""
    count = 0
    for msg in result.all_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    count += 1
    return count


@dataclass
class AgentRunRecord:
    agent_name: str
    model: str
    # NATIVE token counts (provider tokenizer) straight from result.usage() — the
    # response-body usage field. Proven native, not normalized: response usage equals
    # the Generation API's native_tokens_* (see git history / _map_usage). Captured at
    # run time, so they are complete and immune to Generation-API indexing lag. This is
    # what OpenRouter bills on, so it's the authoritative basis for cost. Always valid.
    input_tokens: int = 0
    output_tokens: int = 0
    # NORMALIZED (model-agnostic, GPT-tokenizer) counts from the OpenRouter Generation
    # API (tokens_prompt / tokens_completion). Use these to compare "effort" across
    # models on a common token unit. None means INVALID: the Generation API didn't
    # return one resolved record per LLM round-trip (indexing lag or a missing
    # x-generation-id header), so the normalized sum is incomplete and must not be
    # trusted. Native counts and cost above are unaffected.
    normalized_input_tokens: int | None = 0
    normalized_output_tokens: int | None = 0
    # False ⇒ normalized_input/output_tokens above are None (invalid). See note above.
    normalized_complete: bool = True
    llm_requests: int = 0   # number of LLM API round-trips (>1 when tool loops occur)
    tool_calls: int = 0     # number of MCP/tool call invocations
    duration_s: float = 0.0
    cost_usd: float = 0.0   # modeled cost = native tokens × advertised rate (0.0 if pricing unavailable)
    # True ⇒ native counts were unavailable (usage() returned nothing) and the price
    # fell back to a normalized estimate. Normally False — native is always present.
    cost_estimated: bool = False


@dataclass
class ExperimentSession:
    session_id: str
    started_at: str         # ISO 8601
    model: str              # model used for all agents in this session
    user_query: str
    scenario: str           # optional label for structured experiments (e.g. "bgp-flap-01")
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    run_id: str = ''        # UUID shared by every session in one experiment_runner invocation
    # First / last OpenRouter generation ID seen across the whole run (run_id). The same
    # two values are written on every session sharing this run_id. Purely for manual
    # lookup on OpenRouter afterwards — NOT used by any visualization or statistic.
    first_generation_id: str = ''
    last_generation_id: str = ''
    output: str = ''        # orchestrator's final natural-language answer
    # Filled by finalize_costs() / finish_session()
    duration_s: float = 0.0   # summed LLM generation_time across all runs (seconds)
    # Native session totals — from result.usage(). Always valid (the authoritative
    # token count and the basis for cost).
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # Normalized session totals — from the Generation API. None (invalid) if ANY run's
    # normalized count is invalid, since a partial cross-agent sum would be wrong.
    total_normalized_input_tokens: int | None = 0
    total_normalized_output_tokens: int | None = 0
    normalized_complete: bool = True         # False ⇒ normalized totals are None (invalid)
    total_cost_usd: float = 0.0
    cost_estimated: bool = False             # True ⇒ price fell back to a normalized estimate
    total_tool_calls: int = 0
    total_llm_requests: int = 0
    invalid_commands: int = 0
    success: bool = True
    error: str = ''


# Maps id(AgentRunRecord) → generation IDs for that run, populated by record_agent_run.
# Consumed and cleared by finalize_costs().
_run_gen_ids: dict[int, list[str]] = {}

# Serializes mutations of the shared ExperimentSession when sub-agents run
# concurrently. Today the body has no awaits so it's already atomic; the lock
# keeps it correct even if a future edit introduces an await mid-update.
_session_lock = asyncio.Lock()


def begin_session(
    user_query: str,
    model: str,
    scenario: str = '',
    run_id: str = '',
) -> ExperimentSession:
    """Start a new experiment session. Call before running the orchestrator."""
    session_id = uuid.uuid4().hex
    if not scenario:
        scenario = os.getenv('EXPERIMENT_SCENARIO', '')
    return ExperimentSession(
        session_id=session_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        model=model,
        user_query=user_query,
        scenario=scenario,
        run_id=run_id,
    )


async def record_agent_run(
    session: ExperimentSession,
    agent_name: str,
    model: str,
    result: AgentRunResult,
    gen_ids: list[str] | None = None,
) -> None:
    """Record one sub-agent or orchestrator run into the active session.

    Tokens, cost, and duration start as 0 and are filled in later by finalize_costs()
    using the OpenRouter Generation API. gen_ids links this run to its API records.
    """
    usage = result.usage()
    tool_calls = _count_tool_calls(result)

    record = AgentRunRecord(
        agent_name=agent_name,
        model=model,
        # Native counts from usage() are authoritative and available now — store them
        # immediately. They're complete regardless of any later Generation-API lag.
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        # normalized_* filled in finalize_costs() from the Generation API.
        llm_requests=usage.requests or 0,
        tool_calls=tool_calls,
        duration_s=0.0,
        cost_usd=0.0,
    )
    async with _session_lock:
        session.agent_runs.append(record)
        _run_gen_ids[id(record)] = list(gen_ids) if gen_ids else []


def finish_session(
    session: ExperimentSession,
    success: bool = True,
    error: str = '',
) -> None:
    """Aggregate token totals, persist to JSONL, and emit a Logfire span.

    Call finalize_costs() before this to populate total_cost_usd and duration_s
    (summed generation_time) from OpenRouter.
    """
    session.success = success
    session.error = error

    for run in session.agent_runs:
        session.total_tool_calls += run.tool_calls
        session.total_llm_requests += run.llm_requests
    # Native token totals come from result.usage(), which is populated on every run, so
    # aggregate them here — this keeps them correct even when finalize_costs() returns
    # early (no generation IDs captured). finalize_costs() recomputes the same sums and
    # additionally fills the normalized totals, cost, and duration from the Generation API.
    session.total_input_tokens  = sum(r.input_tokens for r in session.agent_runs)
    session.total_output_tokens = sum(r.output_tokens for r in session.agent_runs)

    EXPERIMENT_LOG.parent.mkdir(exist_ok=True)
    with open(EXPERIMENT_LOG, 'a') as f:
        f.write(json.dumps(asdict(session)) + '\n')

    logfire.info(
        'experiment_session',
        session_id=session.session_id,
        run_id=session.run_id,
        scenario=session.scenario,
        model=session.model,
        user_query=session.user_query[:300],
        output=session.output[:1000],
        duration_s=session.duration_s,
        total_input_tokens=session.total_input_tokens,
        total_output_tokens=session.total_output_tokens,
        total_normalized_input_tokens=session.total_normalized_input_tokens,
        total_normalized_output_tokens=session.total_normalized_output_tokens,
        normalized_complete=session.normalized_complete,
        total_cost_usd=session.total_cost_usd,
        cost_estimated=session.cost_estimated,
        total_tool_calls=session.total_tool_calls,
        total_llm_requests=session.total_llm_requests,
        agent_count=len(session.agent_runs),
        agents_invoked=[r.agent_name for r in session.agent_runs],
        success=session.success,
        error=session.error,
    )


async def finalize_costs(sessions: list[ExperimentSession], api_key: str) -> None:
    """Fetch native token counts and duration from OpenRouter, then model the cost.

    Called once after all turns complete so generation records have had time to be
    indexed. Retries with backoff, then sets per-run and session-level totals.

    Cost is *not* taken from OpenRouter's billed `total_cost`; instead it's modeled
    as native_tokens × the model's advertised per-token rate (model_config.pricing_for).
    See the module docstring for why.
    """
    # Collect every unique generation ID across all runs in all sessions, in
    # chronological order. The first/last are stamped onto every session (run-level
    # metadata, for manual OpenRouter lookup); the set is also what we fetch below.
    all_ids = sorted(
        {
            gid
            for session in sessions
            for run in session.agent_runs
            for gid in _run_gen_ids.get(id(run), [])
        },
        key=_gen_id_sort_key,
    )
    first_id = all_ids[0] if all_ids else ''
    last_id = all_ids[-1] if all_ids else ''
    for session in sessions:
        session.first_generation_id = first_id
        session.last_generation_id = last_id
    if not all_ids:
        return

    async def _fetch_one(client: httpx.AsyncClient, gen_id: str) -> dict:
        resp = await client.get(
            _OPENROUTER_GENERATION_URL,
            params={'id': gen_id},
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=10.0,
        )
        return resp.json().get('data', {}) if resp.status_code == 200 else {}

    # Poll with backoff until all IDs resolve or we give up.
    id_to_data: dict[str, dict] = {}
    pending = list(all_ids)

    async with httpx.AsyncClient() as client:
        # Poll with backoff. Loop exits as soon as every captured ID resolves; the
        # extra later rounds only cost wall-time when a generation is genuinely
        # lagging behind OpenRouter's indexing.
        for delay in (5, 10, 20, 30, 30, 60):
            await asyncio.sleep(delay)
            still_pending = []
            for gen_id in pending:
                try:
                    data = await _fetch_one(client, gen_id)
                    if data.get('tokens_prompt') is not None:
                        id_to_data[gen_id] = data
                    else:
                        still_pending.append(gen_id)
                except Exception:
                    still_pending.append(gen_id)
            pending = still_pending
            if not pending:
                break

    # Advertised pricing per model, fetched once per unique model id (cached upstream).
    pricing_cache: dict[str, ModelPricing | None] = {}

    async def _get_pricing(model_name: str) -> ModelPricing | None:
        if model_name not in pricing_cache:
            pricing_cache[model_name] = await pricing_for(model_name)
        return pricing_cache[model_name]

    # Apply fetched data to each agent run, then compute session totals.
    for session in sessions:
        for run in session.agent_runs:
            run_ids = _run_gen_ids.pop(id(run), [])
            records = [id_to_data[gid] for gid in run_ids if gid in id_to_data]
            # NORMALIZED counts come only from the Generation API. Enrichment too:
            # reasoning split (for cost) and generation_time (for duration).
            norm_input       = sum(int(d.get('tokens_prompt') or 0) for d in records)
            norm_output      = sum(int(d.get('tokens_completion') or 0) for d in records)
            reasoning_tokens = sum(int(d.get('native_tokens_reasoning') or 0) for d in records)
            run.duration_s   = round(sum(int(d.get('generation_time', 0) or 0) for d in records) / 1000, 3)

            # Reconcile against Pydantic AI's own round-trip counter: each LLM request
            # should yield exactly one resolved generation. Fewer means a generation was
            # dropped (indexing lag or a missing x-generation-id header), so the
            # normalized sum is incomplete and can't be trusted — mark it invalid (None)
            # rather than report a wrong number. Native counts (from usage()) and cost
            # are unaffected: usage() is complete regardless of Generation-API lag.
            run.normalized_complete = run.llm_requests > 0 and len(records) >= run.llm_requests
            if run.normalized_complete:
                run.normalized_input_tokens  = norm_input
                run.normalized_output_tokens = norm_output
            else:
                run.normalized_input_tokens  = None  # invalid: a generation is missing
                run.normalized_output_tokens = None

            # Modeled cost from NATIVE tokens (what OpenRouter bills on) × the model's
            # advertised per-token rate — provider-independent and cache-independent.
            # Native is always present, so the price is accurate; only if usage()
            # returned nothing do we fall back to a normalized estimate and flag it.
            if run.input_tokens or run.output_tokens:
                cost_in, cost_out, cost_reasoning = run.input_tokens, run.output_tokens, reasoning_tokens
                run.cost_estimated = False
            elif run.normalized_complete and (norm_input or norm_output):
                cost_in, cost_out, cost_reasoning = norm_input, norm_output, 0
                run.cost_estimated = True
            else:
                cost_in, cost_out, cost_reasoning = 0, 0, 0
                run.cost_estimated = False

            pricing = await _get_pricing(run.model)
            run.cost_usd = round(
                pricing.cost_for(cost_in, cost_out, cost_reasoning, run.llm_requests), 8
            ) if pricing else 0.0

        # Native session totals are always valid (from usage()).
        session.total_input_tokens  = sum(r.input_tokens for r in session.agent_runs)
        session.total_output_tokens = sum(r.output_tokens for r in session.agent_runs)
        session.cost_estimated = any(r.cost_estimated for r in session.agent_runs)
        # Normalized session totals are valid only if every run's normalized count is
        # valid; otherwise a cross-agent sum would be short, so report None (invalid).
        session.normalized_complete = all(r.normalized_complete for r in session.agent_runs)
        if session.normalized_complete:
            session.total_normalized_input_tokens  = sum(r.normalized_input_tokens for r in session.agent_runs)
            session.total_normalized_output_tokens = sum(r.normalized_output_tokens for r in session.agent_runs)
        else:
            session.total_normalized_input_tokens  = None
            session.total_normalized_output_tokens = None
        session.total_cost_usd = round(sum(r.cost_usd for r in session.agent_runs), 8)
        # Session latency = summed LLM generation_time across all runs (pure inference
        # time, model-attributable). Replaces wall-clock, which was contaminated by the
        # cost-polling sleeps above and by batch ordering in multi-turn runs.
        session.duration_s          = round(sum(r.duration_s for r in session.agent_runs), 3)
