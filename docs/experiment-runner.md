# Experiment Runner

## Purpose

`experiment_runner.py` is a terminal-based tool for running structured LLM comparison experiments. Unlike the web UI path, it calls `orchestrator.run()` directly, which means it can capture the orchestrator's own token usage in addition to all sub-agent usage — giving you a complete cost and latency breakdown for every query.

Results are appended to the same `experiment_log.jsonl` file used by the web UI tracker.

---

## When to use this vs. the web UI

| | Web UI | Experiment Runner |
|---|---|---|
| Orchestrator tokens tracked | No (Logfire only) | **Yes** |
| Sub-agent tokens tracked | Yes | Yes |
| Scriptable / batch mode | No | Yes |
| Reproducible identical queries | No | Yes |
| Good for demos | Yes | No |

Use the experiment runner for any run where you want to compare models on identical inputs. Use the web UI for interactive exploration and demos.

---

## End-to-end workflow

The full research loop for "did model X correctly identify the injected fault?":

1. **Author a scenario** under `scenarios/<name>/` — see [Scenario folder layout](#scenario-folder-layout). At minimum: `queries.txt` and `ground_truth.yaml`. Add `setup.sh` / `teardown.sh` to apply and revert the fault automatically.
2. **Run the experiment** with `experiment_runner.py`. The runner applies the fault (via `setup.sh`), executes every query in `queries.txt`, and reverts on the way out — appending one row per turn to `experiment_log.jsonl`. Repeat with each model you want to compare.
3. **Evaluate by hand** with `evaluate_experiments.py`. There is no LLM judge — the script walks you through each session's final answer (showing the scenario `root_cause` as a reference), you press `f`ound / `m`issed / `s`kip, and your binary verdicts (`found_issue` + an optional note) are written to `evaluation_log.jsonl`.
4. **Aggregate** with `analyze_experiments.py`. Joins your verdicts into the per-(scenario, model) summary so `found_rate` shows up next to tokens / cost / latency. `raw_experiments_html.py` renders a sortable per-session HTML view with each row coloured green (found) / red (missed). For a read-only side-by-side of answers and verdicts, `evaluate_experiments.py --review` renders `evaluation_review.md`.

Steps 2–4 are decoupled — running an experiment never evaluates it, re-evaluating after changing your mind (`--redo`) does not require re-running experiments, and re-aggregating does not require re-evaluating.

---

## Usage

```bash
# Folder-based scenario (recommended) — runner loads scenarios/<name>/queries.txt,
# runs setup.sh before the queries, runs teardown.sh after (best-effort).
uv run python experiment_runner.py --model anthropic/claude-sonnet-4.6 --scenario basic-client-communication

# Multi-turn — carry conversation history between queries in the same run
uv run python experiment_runner.py --model z-ai/glm-5 --scenario basic-client-communication --multi-turn

# Skip setup/teardown — sanity run against the unbroken topology
uv run python experiment_runner.py --model z-ai/glm-5 --scenario basic-client-communication --no-fault

# Legacy flat file (still works for ad-hoc query lists outside scenarios/)
uv run python experiment_runner.py --model z-ai/glm-5 --scenario adhoc --file path/to/queries.txt
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--model` / `-m` | `z-ai/glm-5` | OpenRouter model ID for all agents |
| `--scenario` / `-s` | `$EXPERIMENT_SCENARIO` or `""` | Scenario name. If `scenarios/<name>/` exists, queries.txt is loaded and setup.sh/teardown.sh are run automatically. |
| `--file` / `-f` | *(interactive)* | Path to query file (one query per line). Ignored when `--scenario` resolves to a folder. |
| `--multi-turn` | off | Carry message history between queries |
| `--no-fault` | off | Skip the scenario's setup.sh / teardown.sh (for baseline / sanity runs) |

---

## Comparing models on the same scenario

Run the same scenario with each model you want to compare:

```bash
uv run python experiment_runner.py -m z-ai/glm-5                   -s basic-client-communication
uv run python experiment_runner.py -m anthropic/claude-sonnet-4.6  -s basic-client-communication
uv run python experiment_runner.py -m google/gemini-2.0-flash-001  -s basic-client-communication
```

All three runs share the same `scenario` label (and the same setup.sh / teardown.sh,
so each model faces the identical injected fault). The `model` field differs.

---

## Analysing results with Python and pandas

### Load the log

```python
import json
import pandas as pd

records = []
with open('experiment_log.jsonl') as f:
    for line in f:
        records.append(json.loads(line))

df = pd.DataFrame(records)
```

### Join the manual verdicts

To analyse correctness alongside metrics, load `evaluation_log.jsonl` and merge on
`session_id`. Only the final (concluding) turn of each run is evaluated, so
un-evaluated turns get `found_issue = NaN`.

```python
verdicts = []
with open('evaluation_log.jsonl') as f:
    for line in f:
        verdicts.append(json.loads(line))
vdf = pd.DataFrame(verdicts)

df = df.merge(vdf[['session_id', 'found_issue']], on='session_id', how='left')

# found_rate across evaluated turns only — don't include un-evaluated turns in the
# denominator or models with multi-turn runs look artificially worse.
evaluated = df[df['found_issue'].notna()]
found_rate = (
    evaluated.groupby(['scenario', 'model'])['found_issue']
    .apply(lambda s: s.astype(bool).mean())
    .round(2)
    .rename('found_rate')
)
print(found_rate)
```

`analyze_experiments.py` already does this join and adds `found_rate` and
`evaluated_turns` columns to its scenario summary — so most readers will run that
instead of writing pandas by hand.

### Top-level comparison — one row per (scenario, model)

```python
summary = (
    df.groupby(['scenario', 'model'])
    .agg(
        turns            = ('session_id',          'count'),
        avg_duration_s   = ('duration_s',           'mean'),
        avg_input_tokens = ('total_input_tokens',   'mean'),
        avg_output_tokens= ('total_output_tokens',  'mean'),
        avg_tool_calls   = ('total_tool_calls',     'mean'),
        total_cost_usd   = ('total_cost_usd',       'sum'),
        success_rate     = ('success',              'mean'),
    )
    .round(3)
)
print(summary)
```

### Per-agent breakdown — orchestrator vs. sub-agents

Each session record has an `agent_runs` list. Explode it into a flat table:

```python
agent_rows = []
for rec in records:
    for run in rec['agent_runs']:
        agent_rows.append({
            'session_id'   : rec['session_id'],
            'scenario'     : rec['scenario'],
            'model'        : rec['model'],
            'agent_name'   : run['agent_name'],
            'input_tokens' : run['input_tokens'],
            'output_tokens': run['output_tokens'],
            'tool_calls'   : run['tool_calls'],
            'duration_s'   : run['duration_s'],
            'cost_usd'     : run['estimated_cost_usd'],
        })

agents_df = pd.DataFrame(agent_rows)

# Average tokens and duration split by agent, per model
print(
    agents_df.groupby(['model', 'agent_name'])[
        ['input_tokens', 'output_tokens', 'tool_calls', 'duration_s']
    ].mean().round(1)
)
```

### Orchestrator cost share

How much of the total cost comes from the orchestrator itself vs. the sub-agents?

```python
orch = agents_df[agents_df['agent_name'] == 'orchestrator']
subs = agents_df[agents_df['agent_name'] != 'orchestrator']

print("Orchestrator avg cost per turn:")
print(orch.groupby('model')['cost_usd'].mean())

print("\nSub-agents avg cost per turn:")
print(subs.groupby('model')['cost_usd'].mean())
```

### Slowest turns

```python
print(
    df.nlargest(10, 'duration_s')[
        ['scenario', 'model', 'duration_s', 'total_tool_calls', 'success']
    ]
)
```

### Failed turns

```python
failed = df[~df['success']]
if failed.empty:
    print('No failures.')
else:
    print(failed[['scenario', 'model', 'user_query', 'error']])
```

---

## Scenario folder layout

A scenario is a directory under `scenarios/` containing everything the experiment needs:

```
scenarios/bgp-router1-router2-down/
├── queries.txt          # one query per line (# = comment)
├── ground_truth.yaml    # the root cause you check answers against (see below)
├── setup.sh             # introduces the fault; runs before the queries
└── teardown.sh          # restores the baseline; runs after, best-effort
```

`setup.sh` and `teardown.sh` are optional — omit them for baseline scenarios that
probe the unbroken topology. `--no-fault` skips them even when present.

### `ground_truth.yaml`

Your reference while evaluating (see [Manual correctness evaluation](#manual-correctness-evaluation)).
It is a single `root_cause` field — a paragraph describing what a correct diagnosis
must identify:

```yaml
# scenario: bgp-router1-router2-down
root_cause: >
  Interface ethernet-1/2 (e1-2) on router1 is administratively disabled, which tore
  down the BGP session to router2 — so client1 cannot reach client3. A correct answer
  pins the problem to router1's e1-2 being shut down (not a router2 config error, DNS,
  or a switch fault).
```

For a baseline (healthy) scenario, describe the expected healthy state instead:

```yaml
# scenario: basic-client-communication  (baseline — no fault)
root_cause: >
  No fault. The network is healthy and client1, client2 and client3 can all reach
  each other. A correct answer reports full reachability and invents no fault.
```

You evaluate only the final answer of each run (the concluding turn of a multi-turn
investigation), recording a binary verdict: `found_issue` + an optional note. Make the
`root_cause` specific — name the device/interface and the likely wrong answers — so
when you're labelling you don't credit a hedged guess as a find. It's optional: a
scenario without `ground_truth.yaml` can still be evaluated by hand, you just won't get
the reference printed alongside the answer.

### Provided scenarios

| Folder | Fault | Description |
|---|---|---|
| `scenarios/basic-client-communication/` | none | Probes end-to-end reachability between client1, client2 and client3 in the healthy topology |

This is the baseline (no-fault) scenario. The 10 fault scenarios live alongside it in
[../scenarios/](../scenarios/) — see [../scenarios/README.md](../scenarios/README.md) and
[../scenarios/scenario-guide.md](../scenarios/scenario-guide.md). Author new fault scenarios
by creating a folder with all four files above.

---

## Manual correctness evaluation

`evaluate_experiments.py` has no LLM judge — you score correctness yourself. It walks
through every un-evaluated session in `experiment_log.jsonl`, prints the query, the
agent's full final answer, and the scenario's `root_cause` as a reference, and waits
for your verdict. Each call writes one row (`found_issue` + an optional note) to
`evaluation_log.jsonl`, keyed by `session_id`.

```bash
# Walk through every session without a verdict yet
uv run python evaluate_experiments.py

# Re-evaluate everything, even sessions already labelled (you changed your mind)
uv run python evaluate_experiments.py --redo

# Filter by date, model, or scenario
uv run python evaluate_experiments.py --since 2026-05-20
uv run python evaluate_experiments.py --model anthropic/claude-sonnet-4.6
uv run python evaluate_experiments.py --scenario bgp-router1-router2-down
```

At each prompt: `[f]ound  [m]issed  [s]kip  [q]uit`. Append free text after the letter
to attach a note (`f named the right interface`). Verdicts are persisted immediately,
so quitting half-way never loses progress. Only the final (concluding) turn of each
run is offered for evaluation.

### Re-reading your verdicts

`--review` / `--review-csv` render a read-only join of answers and the verdicts you've
recorded (no prompts, no model calls) — useful for re-reading or for a thesis appendix:

```bash
# Markdown report of all evaluated sessions → evaluation_review.md
uv run python evaluate_experiments.py --review

# Random sample N sessions (reproducible via --sample-seed)
uv run python evaluate_experiments.py --review --sample 30 --sample-seed 42

# CSV companion (answers + verdicts) → evaluation_review.csv
uv run python evaluate_experiments.py --review-csv
```

Each entry in `evaluation_review.md` shows the query, the agent's full answer, and your
verdict (FOUND ISSUE ✓ / DID NOT FIND ISSUE ✗) with any note you attached.

---

## Per-turn output

Each query produces a summary table printed to the terminal:

```
────────────────────────────────────────────────────────────────────────
  Turn 1/3  |  Check the BGP session state on all routers...
────────────────────────────────────────────────────────────────────────
  Agent                Model                             In tok  Out tok  Tools      s
  ──────────────────── ────────────────────────────────  ───────  ───────  ─────  ──────
  network_agent        anthropic/claude-sonnet-4.6         4,821      412      4     9.2
  orchestrator         anthropic/claude-sonnet-4.6         1,204      183      2    14.8
────────────────────────────────────────────────────────────────────────
  Total: 6,025 in / 595 out  |  6 tool calls  |  14.8s  |  $0.000224  |  OK
```

Followed by a session summary at the end of all queries:

```
════════════════════════════════════════════════════════════════════════
  EXPERIMENT SUMMARY
  Model:    anthropic/claude-sonnet-4.6
  Scenario: bgp-01
  Turns:    3  (3 successful)
════════════════════════════════════════════════════════════════════════
  Avg duration:     12.4s
  Total tokens:    18,120 in  /  1,840 out
  Total tool calls: 14
  Estimated cost:  $0.000731
════════════════════════════════════════════════════════════════════════
  Results appended to: experiment_log.jsonl
```

---

## What is tracked vs. the web UI

In the runner, each session record in `experiment_log.jsonl` includes both the
final natural-language answer (`output`) and a `run_id` shared across all turns
of the same invocation, plus an `agent_runs` list with an entry for the
orchestrator alongside the sub-agents:

```json
{
  "session_id": "abc12345...",
  "run_id":     "f0e1d2c3...",         // shared by every turn in one invocation
  "model":      "anthropic/claude-sonnet-4.6",
  "scenario":   "bgp-router1-router2-down",
  "user_query": "Summarize the root cause.",
  "output":     "Interface e1-2 on router1 is admin-down, which has torn down...",
  "agent_runs": [
    { "agent_name": "network_agent",  "input_tokens": 4821, "tool_calls": 4 },
    { "agent_name": "orchestrator",   "input_tokens": 1204, "tool_calls": 2 }
  ],
  "success": true,
  "error":   ""
}
```

The orchestrator entry in `agent_runs` captures the LLM calls made by the
orchestrator itself — deciding which tool to call, routing between sub-agents,
and composing the final answer. These are the calls that cannot be captured via
the web UI path.

The `output` field is what `evaluate_experiments.py` shows you when evaluating
correctness. The `run_id` field lets it group multi-turn sessions and offer
only the final (concluding) answer of each invocation for evaluation.

---

## Limitations

- **Syslog investigator not tracked**: Background LLM investigation tasks (`syslog_investigator`) run independently and are not part of a query's session record. Their usage is captured in Logfire.
- **All sub-agents start**: `main_lifespan()` starts MCP servers for all agents (network, config, syslog, topology, snapshot). There is currently no way to start only a subset.
- **No streaming output**: The orchestrator's answer is printed only after the full run completes, unlike the web UI which streams tokens live.