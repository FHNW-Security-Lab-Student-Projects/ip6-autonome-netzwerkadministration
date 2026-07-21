# Wall-Clock Recovery from Logfire

## Purpose

Explains how the true per-session wall-clock durations in
`wall_clock_durations.jsonl` are recovered from Logfire traces, how the
involved files relate to each other, how the recovery is *proven* correct
(`join_verification.jsonl`), and what to execute when.

## Why recovery is needed at all

`experiment_log.jsonl`'s `duration_s` is **not** wall-clock time. It is the
sum of OpenRouter `generation_time` over all LLM round-trips of a session
(see `experiment_tracker.py`, `finalize_costs`) — pure inference time. It
excludes tool execution, MCP round-trips and delegation overhead, and because
sub-agents run **in parallel**, the sum can *exceed* the real elapsed time
(median `inference/wall` is up to ~147% per model). It must never be
presented as "troubleshooting time".

The real wall clock is recoverable without rerunning anything: every run was
traced to Logfire (`logfire.instrument_pydantic_ai` in `client_agent.py`),
and each session produced exactly one root `orchestrator run` span whose
start/end timestamps bracket the whole investigation.

## The files and how they relate

```
                       Logfire  (cloud, retention-limited!)
                          │
                          │  fetch_logfire_exports.py          ← needs LOGFIRE_READ_TOKEN in .env
                          │  (Logfire /v1/query HTTP API)
            ┌─────────────┴──────────────────┐
            ▼                                ▼
 logfire_root_spans.jsonl        logfire_generation_ids.jsonl      (checked-in exports)
 the MEASUREMENT:                the PROOF DATA:
 ["start","end","trace_id"]      [gen_id, trace_id, pos, n_gens]
 per root 'orchestrator          for every first/last generation
 run' span                       ID in experiment_log.jsonl
            │                                │
            │       experiment_log.jsonl     │
            │       (started_at, first/last_generation_id per session)
            │                │               │
            └────────────────┼───────────────┘
                             ▼
                  recover_wall_clock.py          ← reads ONLY local files
                  1. timestamp join  (session.started_at ↔ span start, ±10 s)
                  2. gen-ID verification of every match
                             │
            ┌────────────────┴───────────────┐
            ▼                                ▼
 wall_clock_durations.jsonl        join_verification.jsonl
 per session: wall_clock_s,        per session: which trace each
 inference_s, overhead_s,          generation ID resolved to and at
 trace_id, gen_id_match,           which position → the proof the
 join_verified                     join is correct
            │
            ▼
 analyze_experiments.py / visualize_experiments.py
 (join wall_clock_durations.jsonl by session_id; time
  figures use wall_clock_s, not duration_s)
```

The two Logfire exports answer **different questions and both are required**:

| File | Role | Contains |
|---|---|---|
| `logfire_root_spans.jsonl` | measurement | one `["start","end","trace_id"]` per root span — the only source of elapsed time |
| `logfire_generation_ids.jsonl` | verification | one `[gen_id, trace_id, pos, n_gens]` per experiment generation ID — no timing at all, only "which trace holds this ID, at which chronological rank" |

They are checked in because **Logfire retention expires**: once the
experiment window (runs of 2026-06-29 … 2026-07-06) falls out of retention,
the exports can no longer be regenerated and the checked-in copies are the
only surviving evidence.

## Step 1 — the timestamp join

Logfire scrubs `session_id` from its own records, so the join cannot be done
by ID. Instead `recover_wall_clock.py` matches each session to the nearest
unused root span whose start lies within ±10 s of the session's
`started_at` (`JOIN_WINDOW_S`). In practice the root span starts ~0.1 s
after `started_at` and sessions are minutes apart, so the match is
unambiguous — all 300 sessions match 1:1 (20 of the 320 exported root spans
belong to no experiment session; they are dev/interactive runs and stay
unclaimed).

Per session it derives:

| Field | Meaning |
|---|---|
| `wall_clock_s` | root-span end − session `started_at` — the real elapsed time |
| `inference_s` | the log's `duration_s` (summed generation_time), for contrast |
| `overhead_s` | `wall_clock_s − inference_s` (negative ⇒ parallel LLM calls) |
| `span_start_delta_s` | join sanity value: span start − `started_at` |
| `trace_id` | the matched Logfire trace |

## Step 2 — generation-ID verification of the join

A timestamp join alone is circumstantial. The verification makes it exact:
OpenRouter generation IDs are **globally unique**, and `experiment_log.jsonl`
records each session's `first_generation_id` / `last_generation_id`
(captured from the `X-Generation-Id` response headers at run time). Both IDs
are looked up in `logfire_generation_ids.jsonl` (Logfire records them as the
span attribute `gen_ai.response.id`) and **must resolve to exactly the
timestamp-joined trace**. Two IDs per session landing in the right trace
prove the match; a single mismatch would flag a broken join.

Expected match classes:

| `gen_id_match` | Meaning | Expected for |
|---|---|---|
| `exact` | first ID is the trace's 1st generation (`pos == 1`) and last ID its final one (`pos == n_gens`) | all completed runs (245/300) |
| `contained` | both IDs inside the trace, but not at its edges | exactly the DNF runs (55/300) |
| `wrong-trace` / `missing-id` | verification FAILED — the script refuses to write any output | never |

Why DNF runs can only be `contained`, by construction: when the 600 s cap
fires, `asyncio.timeout` aborts `_run_turn` in `experiment_runner.py`
*before* `record_agent_run('orchestrator', …)` executes, so the
orchestrator's own generation IDs are dropped from the log while remaining
in the trace. The log's first/last IDs then come from completed sub-agent
runs only — the trace's true first generation (the orchestrator's opening
call) precedes the log's first ID, hence "contained". This is a property of
the logging path, not a data-quality problem.

The per-session evidence is written to `join_verification.jsonl`:

```json
{"session_id": "…", "run_id": "…", "scenario": "…", "model": "…",
 "trace_id": "019f…", "span_start_delta_s": 0.054,
 "first_generation_id": "gen-…", "first_id_trace": "019f…", "first_id_pos": 1,
 "last_generation_id":  "gen-…", "last_id_trace":  "019f…", "last_id_pos": 16,
 "trace_generations": 16, "match": "exact", "verified": true}
```

and summarized into each `wall_clock_durations.jsonl` record as
`gen_id_match` + `join_verified`.

Current verified state (2026-07-06): **300 matched, 245 exact, 55 contained,
0 failures.**

## What to execute when

All commands run inside the devcontainer (`uv run …`).

**Normal case — exports are checked in (this is where you start):**

```bash
uv run python recover_wall_clock.py            # rewrites wall_clock_durations.jsonl
                                               # + join_verification.jsonl
uv run python recover_wall_clock.py --dry-run  # summary only, writes nothing
```

`recover_wall_clock.py` reads **only local files** and needs no credentials.
Expect the summary line `generation-ID verification: contained: 55  exact: 245`;
if any session fails verification the script exits without writing.

**Regenerating the Logfire exports (new experiments, or re-deriving the
checked-in files from the authoritative source):**

1. Create a **read token** in the Logfire project settings (the write
   `LOGFIRE_TOKEN` and the `.mcp.json` MCP token are *not* accepted by the
   query API) and put it into `.env`:

   ```
   LOGFIRE_READ_TOKEN=pylf_v1_eu_…
   ```

2. Fetch, then recover:

   ```bash
   uv run python fetch_logfire_exports.py   # rewrites logfire_root_spans.jsonl
                                            #        + logfire_generation_ids.jsonl
   uv run python recover_wall_clock.py      # join + verify + write outputs
   ```

   Do this **while Logfire retention still covers the experiment window** —
   afterwards the checked-in exports are the only copy.

**Downstream:** `analyze_experiments.py` and `visualize_experiments.py` pick
up `wall_clock_durations.jsonl` automatically (joined by `session_id`); the
time-based figures use `wall_clock_s`. Re-run them after re-running the
recovery.

## Interpretation caveats for the report

- **Right-censoring:** the 55 DNF runs were cancelled by the matrix runner's
  600 s cap (`MAX_SECONDS` in `run_experiment_matrix.sh`), so their
  `wall_clock_s` is right-censored at ~600 s. deepseek's median sits at the
  cap (>50% of its runs timed out) — report it as "≥ 600 s", not as a
  measured median.
- **`overhead_s` can be negative** — that is not an error. Inference time is
  summed over concurrently running agents, so it exceeds wall clock whenever
  the orchestrator fans out sub-agents in parallel.
- **`duration_s` from `experiment_log.jsonl` is inference-only.** Any
  time-savings statement must be based on `wall_clock_s`.

## Related docs

- `experiment-tracker.md` — how sessions, tokens and generation IDs are
  recorded at run time
- `analyze-experiments.md` / `visualize-experiments.md` — the consumers of
  `wall_clock_durations.jsonl`
- `model-pricing.md` — the cost side of the same experiment log
