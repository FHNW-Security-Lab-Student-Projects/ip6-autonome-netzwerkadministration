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
    record_agent_run(session, "network_agent", "z-ai/glm-5", result, gen_ids)
    finish_session(session, success=True)

Cost, tokens, and duration come from the OpenRouter Generation API (actual billed values).
duration_s is summed from generation_time across all LLM round-trips for the run.
"""

import contextlib
import json
import os
import time
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


@dataclass
class GenerationData:
    """Actual billing data from the OpenRouter Generation API, summed across all LLM requests."""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def available(self) -> bool:
        return self.input_tokens > 0 or self.output_tokens > 0 or self.cost_usd > 0.0


@contextlib.contextmanager
def capture_generation_ids() -> Generator[list[str], None, None]:
    """Sync context manager — collects OpenRouter X-Request-Id values during an agent run.

    Usage:
        with capture_generation_ids() as gen_ids:
            result = await agent.run(...)
        gen_data = await fetch_generation_data(api_key, gen_ids)
    """
    ids: list[str] = []
    token = _capturing_ids.set(ids)
    try:
        yield ids
    finally:
        _capturing_ids.reset(token)


async def fetch_generation_data(api_key: str, generation_ids: list[str]) -> GenerationData:
    """Fetch actual cost and token counts from the OpenRouter Generation API.

    Calls GET /api/v1/generation?id=... for each ID and sums:
      total_cost        → cost_usd
      tokens_prompt     → input_tokens
      tokens_completion → output_tokens

    Waits 2 seconds before the first attempt (OpenRouter's backend needs a moment
    to write generation records after a request completes), then retries once after
    another 3 seconds if data is still missing.

    Returns an empty GenerationData if no IDs provided or all fetches fail.
    """
    import asyncio

    if not generation_ids:
        return GenerationData()

    async def _fetch_one(client: httpx.AsyncClient, gen_id: str) -> dict:
        resp = await client.get(
            _OPENROUTER_GENERATION_URL,
            params={'id': gen_id},
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json().get('data', {})
        return {}

    # OpenRouter's backend can take up to ~30s to write the generation record after
    # the request completes. Poll with increasing delays until all IDs resolve.
    result = GenerationData()
    pending = list(generation_ids)

    async with httpx.AsyncClient() as client:
        for delay in (3, 7, 15, 30):
            await asyncio.sleep(delay)
            still_pending = []
            for gen_id in pending:
                try:
                    data = await _fetch_one(client, gen_id)
                    if data.get('tokens_prompt') is not None:
                        result.cost_usd += float(data.get('total_cost', 0.0) or 0.0)
                        result.input_tokens += int(data.get('native_tokens_prompt') or data.get('tokens_prompt', 0) or 0)
                        result.output_tokens += int(data.get('native_tokens_completion') or data.get('tokens_completion', 0) or 0)
                    else:
                        still_pending.append(gen_id)
                except Exception:
                    still_pending.append(gen_id)
            pending = still_pending
            if not pending:
                break

    result.cost_usd = round(result.cost_usd, 8)
    return result


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
    input_tokens: int = 0
    output_tokens: int = 0
    llm_requests: int = 0   # number of LLM API round-trips (>1 when tool loops occur)
    tool_calls: int = 0     # number of MCP/tool call invocations
    duration_s: float = 0.0
    cost_usd: float = 0.0   # actual billed cost from OpenRouter Generation API (0.0 if unavailable)


@dataclass
class ExperimentSession:
    session_id: str
    started_at: str         # ISO 8601
    model: str              # model used for all agents in this session
    user_query: str
    scenario: str           # optional label for structured experiments (e.g. "bgp-flap-01")
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    run_id: str = ''        # UUID shared by every session in one experiment_runner invocation
    output: str = ''        # orchestrator's final natural-language answer
    # Filled by finish_session()
    duration_s: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_tool_calls: int = 0
    total_llm_requests: int = 0
    invalid_commands: int = 0
    success: bool = True
    error: str = ''


_session_start_times: dict[str, float] = {}
# Maps id(AgentRunRecord) → generation IDs for that run, populated by record_agent_run.
# Consumed and cleared by finalize_costs().
_run_gen_ids: dict[int, list[str]] = {}


def begin_session(
    user_query: str,
    model: str,
    scenario: str = '',
    run_id: str = '',
) -> ExperimentSession:
    """Start a new experiment session. Call before running the orchestrator."""
    session_id = uuid.uuid4().hex
    _session_start_times[session_id] = time.monotonic()
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


def record_agent_run(
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
        input_tokens=0,
        output_tokens=0,
        llm_requests=usage.requests or 0,
        tool_calls=tool_calls,
        duration_s=0.0,
        cost_usd=0.0,
    )
    session.agent_runs.append(record)
    _run_gen_ids[id(record)] = list(gen_ids) if gen_ids else []


def finish_session(
    session: ExperimentSession,
    success: bool = True,
    error: str = '',
) -> None:
    """Aggregate token totals, persist to JSONL, and emit a Logfire span.

    Call finalize_costs() before this to populate total_cost_usd from OpenRouter.
    """
    t0 = _session_start_times.pop(session.session_id, None)
    session.duration_s = round(time.monotonic() - t0, 3) if t0 else 0.0
    session.success = success
    session.error = error

    for run in session.agent_runs:
        session.total_tool_calls += run.tool_calls
        session.total_llm_requests += run.llm_requests
    # total_input_tokens, total_output_tokens, total_cost_usd are set by finalize_costs()
    # from the OpenRouter Generation API — do not aggregate from per-run zeros here.

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
        total_cost_usd=session.total_cost_usd,
        total_tool_calls=session.total_tool_calls,
        total_llm_requests=session.total_llm_requests,
        agent_count=len(session.agent_runs),
        agents_invoked=[r.agent_name for r in session.agent_runs],
        success=session.success,
        error=session.error,
    )


async def finalize_costs(sessions: list[ExperimentSession], api_key: str) -> None:
    """Fetch tokens and cost from OpenRouter for every agent run across all sessions.

    Called once after all turns complete so generation records have had time to be
    indexed. Retries with backoff, then sets per-run and session-level totals.
    """
    import asyncio

    # Collect every unique generation ID across all runs in all sessions.
    all_ids = list({
        gid
        for session in sessions
        for run in session.agent_runs
        for gid in _run_gen_ids.get(id(run), [])
    })
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
        for delay in (5, 10, 20, 30):
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

    # Apply fetched data to each agent run, then compute session totals.
    for session in sessions:
        for run in session.agent_runs:
            run_ids = _run_gen_ids.pop(id(run), [])
            records = [id_to_data[gid] for gid in run_ids if gid in id_to_data]
            run.input_tokens  = sum(int(d.get('native_tokens_prompt') or d.get('tokens_prompt', 0) or 0) for d in records)
            run.output_tokens = sum(int(d.get('native_tokens_completion') or d.get('tokens_completion', 0) or 0) for d in records)
            run.cost_usd      = round(sum(float(d.get('total_cost', 0.0) or 0.0) for d in records), 8)
            run.duration_s    = round(sum(int(d.get('generation_time', 0) or 0) for d in records) / 1000, 3)

        session.total_input_tokens  = sum(r.input_tokens for r in session.agent_runs)
        session.total_output_tokens = sum(r.output_tokens for r in session.agent_runs)
        session.total_cost_usd      = round(sum(r.cost_usd for r in session.agent_runs), 8)
