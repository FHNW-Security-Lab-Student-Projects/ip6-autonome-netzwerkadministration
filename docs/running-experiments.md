# How to Run Experiments (Step by Step)

This is the end-to-end runbook for the fault-injection experiment workflow: define a
broken network scenario, run the multi-agent system against it, and evaluate by hand
whether the agents found the real root cause.

> **Related docs:** [experiment-runner.md](experiment-runner.md) (reference for every flag),
> [running-the-system.md](running-the-system.md) (lab + agent startup),
> [visualize-experiments.md](visualize-experiments.md) (charts).

---

## The loop at a glance

```
  ①  Author scenario        scenarios/<name>/{queries.txt, ground_truth.yaml, setup.sh, teardown.sh}
        │
  ②  Run experiment         uv run python experiment_runner.py -m <model> -s <name>
        │                    (or ./run_experiment_matrix.sh for the whole model × scenario matrix)
        │                    → setup.sh → queries → teardown.sh
        │                    → appends rows to experiment_log.jsonl
        │
  ③  Evaluate by hand       uv run python evaluate_experiments.py
        │                    → walks you through each answer; you press f/m/s
        │                    → appends your verdicts to evaluation_log.jsonl
        │
  ④  Aggregate              uv run python analyze_experiments.py        # tables / CSV
        │                    uv run python raw_experiments_html.py       # HTML table
        │                    uv run python visualize_experiments.py      # averaged charts
        │                    uv run python visualize_raw_experiments.py  # per-session chart
        │
  ⑤  Recover wall clock    uv run python recover_wall_clock.py
                             → wall_clock_durations.jsonl (+ join_verification.jsonl)
```

Steps ②–④ are fully decoupled. Running an experiment never evaluates it — the runner
only writes `experiment_log.jsonl`; you score it later, whenever you want. You can
re-evaluate (③) after changing your mind without re-running experiments (`--redo`),
and re-aggregate (④) without re-evaluating.

---

## Prerequisites (one-time setup)

### 1. Environment

```bash
cp .env.example .env        # then set OPENROUTER_API_KEY (LOGFIRE_TOKEN optional)
uv sync                     # install dependencies
```


### 2. Start the network lab

The agents talk to real SR Linux devices, so the lab must be running first:

```bash
sudo containerlab deploy -t testlab.clab.yml
```

The topology (see [testlab.clab.yml](../testlab.clab.yml)):

```
client1 ─ switch1 ─ router1 ═BGP═ router2 ─ switch2 ─ client3
client2 ─┘
```

Containers are named `clab-testlab-<node>` (e.g. `clab-testlab-router1`).

> You do **not** start the agents separately. `experiment_runner.py` boots all
> sub-agent MCP servers itself via `main_lifespan()`.

---

## Step ① — Author a scenario

A scenario is a folder under `scenarios/` holding everything needed to reproduce
one experiment:

```
scenarios/intf-down/
├── queries.txt          # what to ask the agents (one query per line)
├── ground_truth.yaml    # the actual root cause (your evaluation reference)
├── setup.sh             # injects the fault   (optional)
└── teardown.sh          # restores baseline   (optional)
```

### 1a. `queries.txt`

One query per line; `#` lines are comments. For a multi-turn investigation, end
with a query that asks for a final diagnosis — that last answer is what you evaluate.

```text
# scenarios/intf-down/queries.txt
Client1 reports it can no longer reach client3. Investigate the root cause.
```

For a multi-turn investigation, list several queries and end with one that asks for
the final diagnosis — that last answer is what you evaluate:

```text
Are client1 and client3 able to communicate? Investigate.
Check the BGP session state between router1 and router2.
Summarize the root cause of the problem you found.
```

### 1b. `ground_truth.yaml`

This is your reference while evaluating. It is a **single `root_cause` field** describing
what a correct diagnosis must identify. When you evaluate (Step ③), the script prints
this next to the agent's answer so you can decide *did the agent find the issue?* without
re-deriving the right answer each time (see also the baseline examples in
`scenarios/*/ground_truth.yaml`):

```yaml
# scenario: intf-down
root_cause: >
  Interface ethernet-1/2 (e1-2) on router1 is administratively disabled, which tore
  down the BGP session to router2 and removed the cross-router route advertisements —
  so client1 cannot reach client3. A correct answer pins the problem to router1's
  e1-2 interface being shut down (not a router2 config error or a DNS / switch fault).
```

For a **baseline (healthy) scenario**, describe the expected healthy state — a correct
answer reports the network as fine and invents no problem:

```yaml
# scenario: basic-client-communication  (baseline — no fault)
root_cause: >
  No fault. The network is healthy and client1, client2 and client3 can all reach
  each other. A correct answer reports full reachability and invents no fault.
```

Write the `root_cause` to be specific enough that *you* can tell a real diagnosis from
a near-miss at a glance — name the device/interface and, ideally, the obvious wrong
answers, so you don't credit a hedging "it might be DNS or the interface" reply as a
find. Keep it to one paragraph; it's a reminder for your own eyes, not a rubric an LLM
parses. `ground_truth.yaml` is optional — a scenario without one can still be evaluated
by hand, you just won't get the reference printed alongside the answer.

> **What gets evaluated:** only the **final answer of each run** (in a multi-turn
> investigation the earlier turns are exploration). Your verdict is binary —
> `found_issue: true/false`, plus an optional free-text note — and you make the call
> yourself in Step ③.

### 1c. `setup.sh` and `teardown.sh`

`setup.sh` runs **before** the queries and must inject the fault; `teardown.sh`
runs **after** (always, even on crash/Ctrl-C) and must restore the baseline.
Skip both for baseline scenarios, or skip them at runtime with `--no-fault`.

Example — shut down (and later re-enable) `router1` interface `e1-2` over the
SR Linux CLI. The scripts `docker exec` into the node and drive `sr_cli`:

```bash
#!/usr/bin/env bash
# scenarios/intf-down/setup.sh
set -euo pipefail
sudo docker exec clab-testlab-router1 sr_cli -c "enter candidate" \
  -c "set / interface ethernet-1/2 admin-state disable" \
  -c "commit now"
echo "fault injected: router1 ethernet-1/2 admin-disabled"
```

```bash
#!/usr/bin/env bash
# scenarios/intf-down/teardown.sh
set -euo pipefail
sudo docker exec clab-testlab-router1 sr_cli -c "enter candidate" \
  -c "set / interface ethernet-1/2 admin-state enable" \
  -c "commit now"
echo "baseline restored: router1 ethernet-1/2 admin-enabled"
```

```bash
chmod +x scenarios/intf-down/*.sh
```

> **The `sr_cli` invocation above is illustrative — verify the exact syntax for
> your SR Linux image before relying on it** (run the commands interactively once:
> `sudo docker exec -it clab-testlab-router1 sr_cli`). Other valid mechanisms:
> the project's own JSON-RPC helper ([srl_jsonrpc.py](../srl_jsonrpc.py)), or
> `gnmic`. **Always test setup.sh + teardown.sh by hand first**: run `setup.sh`,
> confirm the fault is real (e.g. `ping` between clients fails), then run
> `teardown.sh` and confirm recovery.

---

## Step ② — Run the experiment

```bash
uv run python experiment_runner.py \
  --model anthropic/claude-opus-4.8 \
  --scenario intf-down
```

What happens, in order:

1. `setup.sh` runs (the fault is injected). If it fails, the run aborts before any query.
2. All sub-agent MCP servers start.
3. Each line of `queries.txt` is sent to the orchestrator. `--multi-turn` carries
   conversation history between queries — available, but unused in the recorded
   experiments: all scenarios are single-query.
4. Every turn is appended to `experiment_log.jsonl` (with the final `output` and a
   shared `run_id`).
5. `teardown.sh` runs in a `finally` block — the baseline is restored even if a
   query crashed or you hit Ctrl-C.
6. Costs are fetched from OpenRouter and a summary table prints.

**Useful variants:**

```bash
# Sanity run against the healthy topology (skip setup.sh / teardown.sh)
uv run python experiment_runner.py -m z-ai/glm-5.2 -s intf-down --no-fault

# Compare several models on the identical fault — run once per model, same scenario
uv run python experiment_runner.py -m z-ai/glm-5.2               -s intf-down
uv run python experiment_runner.py -m anthropic/claude-opus-4.8  -s intf-down
uv run python experiment_runner.py -m openai/gpt-5.5             -s intf-down
```

All runs share the `scenario` label (and identical fault); only the `model` differs.

### Automated: the full matrix with `run_experiment_matrix.sh`

The 300 recorded runs were not started by hand — they came from
[run_experiment_matrix.sh](../run_experiment_matrix.sh), which batch-runs
`experiment_runner.py` over a model × scenario matrix:

```bash
./run_experiment_matrix.sh --dry-run        # print the planned runs, execute nothing
./run_experiment_matrix.sh                  # the full sweep
./run_experiment_matrix.sh --resume         # pick up an interrupted sweep
```

Defaults (the CONFIG block at the top of the script — the configuration of
record for the 300 logged runs): 6 models × 10 scenarios × `REPEATS=5`
= 300 runs. The models, two per price tier:

| Tier | Models |
|---|---|
| high | `anthropic/claude-opus-4.8`, `openai/gpt-5.5` |
| mid | `z-ai/glm-5.2`, `qwen/qwen3.7-max` |
| low | `deepseek/deepseek-v3.2`, `mistralai/ministral-14b-2512` |

Per (scenario, model, repeat) the script redeploys the containerlab topology
for a clean lab (`REDEPLOY_BETWEEN_RUNS=1`, then waits `SETTLE_SECONDS=45`
for the SR Linux nodes to boot) and invokes the runner with
`--max-seconds $MAX_SECONDS` (`MAX_SECONDS=600` — see the DNF note below).

Flags: `--models a/b,c/d`, `--scenarios x,y`, `--repeats N`, `--max-seconds N`
override the CONFIG defaults; `--no-redeploy` skips the containerlab redeploy;
`--dry-run` prints the planned commands; `--resume [DIR]` continues a prior sweep.

Bookkeeping lives under `logs/matrix/<timestamp>/`: one console log per run
(`<scenario>__<model>__rN.log`) plus `progress.txt`, to which each successfully
completed combo is appended. `--resume` (default: the most recent dir under
`logs/matrix/`) skips everything already in `progress.txt`, so an interrupted
or partially failed sweep continues without duplicate rows. One failing combo
never aborts the sweep, and Ctrl-C stops cleanly after the current run.

### Timeouts: what `success=false` means (DNF)

`experiment_runner.py --max-seconds N` (env: `EXPERIMENT_MAX_SECONDS`; default
0 = no cap) puts a wall-clock cap on each orchestrator run. On expiry the
session is aborted and logged with `success=false` and
`error="DNF (wall-clock timeout after Ns)"` — completed sub-agent runs keep
their token/cost stats in the record. The matrix ran with a 600 s cap, and
**55 of the 300 recorded runs ended as DNF**: a `success=false` row in
`experiment_log.jsonl` therefore almost always means *timed out*, not
*crashed*.

---

## Step ③ — Evaluate by hand

There is no LLM judge. You read each final answer and call it yourself. The script
just makes that fast: it walks through every un-evaluated session, prints the query,
the agent's full answer, and the scenario's `root_cause` as a reference, then waits
for your verdict.

```bash
uv run python evaluate_experiments.py
```

At each prompt, press one key:

```
  [f]ound   — the agent correctly identified the root cause (for a healthy
              scenario: correctly reported the network as fine)
  [m]issed  — it missed it, blamed the wrong cause, or invented a fault
  [s]kip    — leave this one un-evaluated for now
  [q]uit    — stop; everything you've labelled so far is already saved
```

You can append a free-text note after the letter — e.g. `f named the right interface`
— and it's stored with the verdict. Each verdict is written to `evaluation_log.jsonl`
**immediately**, so quitting half-way never loses progress. Only the **final answer of
each run** is offered for evaluation (earlier turns of a multi-turn investigation are
exploration).

```bash
# Re-evaluate everything, even sessions you've already labelled (changed your mind)
uv run python evaluate_experiments.py --redo

# Narrow the scope
uv run python evaluate_experiments.py --scenario intf-down
uv run python evaluate_experiments.py --model anthropic/claude-opus-4.8
uv run python evaluate_experiments.py --since 2026-05-20
```

### Re-reading your verdicts later

To produce a read-only report joining each answer with the verdict you gave it (handy
for the thesis appendix, or to re-read without re-prompting):

```bash
uv run python evaluate_experiments.py --review                       # → evaluation_review.md
uv run python evaluate_experiments.py --review --sample 30 --sample-seed 42
uv run python evaluate_experiments.py --review-csv                   # → evaluation_review.csv
```

These never prompt and never call any model — they just render what's already in
`evaluation_log.jsonl`.

---

## Step ④ — Aggregate and visualize

```bash
# Per-(scenario, model) summary, now with found_rate alongside cost/latency
uv run python analyze_experiments.py

# Filter to one scenario, export tables to CSV
uv run python analyze_experiments.py --scenario intf-down --csv report

# Sortable per-session HTML table, rows coloured green (found) / red (missed)
uv run python raw_experiments_html.py --out figures/raw_experiments.html

# Vector charts for the thesis (PDF by default) — averaged across runs
uv run python visualize_experiments.py --out figures

# Same data as one bar per session (no averaging), verdict-coloured labels
uv run python visualize_raw_experiments.py --out figures
```

`analyze_experiments.py` auto-joins `evaluation_log.jsonl`, so the summary shows
`evaluated_turns` and `found_rate` (the fraction of evaluated runs where the agent
found the issue). If you haven't evaluated yet, those columns are simply omitted and
the header notes `Evaluated: 0/N`.

All four consumers read the same `experiment_log.jsonl` + `evaluation_log.jsonl`
(plus `wall_clock_durations.jsonl` when present, see Step ⑤):
`analyze_experiments.py` prints/exports tables, `raw_experiments_html.py` renders a
sortable HTML table, and the two `visualize_*.py` scripts emit PDF/PNG figures
(`visualize_experiments.py` averages across runs and writes up to 23 PDFs;
`visualize_raw_experiments.py` shows one bar per session). See
[visualize-experiments.md](visualize-experiments.md) for the full chart reference.

---

## Step ⑤ — Recover wall-clock durations

`duration_s` in `experiment_log.jsonl` is **summed LLM inference time**, not
elapsed time — never present it as "troubleshooting time". The real per-session
elapsed time is recovered from Logfire traces:

```bash
uv run python recover_wall_clock.py            # → wall_clock_durations.jsonl
                                               #   + join_verification.jsonl
```

It reads only local files (the checked-in Logfire exports) and needs no
credentials. `analyze_experiments.py` and `visualize_experiments.py` pick up
`wall_clock_durations.jsonl` automatically. See
[wall-clock-recovery.md](wall-clock-recovery.md) for how the recovery and its
verification work.

---

## Full worked example (copy-paste)

```bash
# 0. one-time
cp .env.example .env                      # set OPENROUTER_API_KEY
uv sync
sudo containerlab deploy -t testlab.clab.yml

# 1. author the scenario folder (queries.txt + ground_truth.yaml + setup.sh + teardown.sh)
#    — see Step ① above; test setup.sh / teardown.sh by hand first.

# 2. run two models on the same fault (or ./run_experiment_matrix.sh for the whole matrix)
uv run python experiment_runner.py -m z-ai/glm-5.2               -s intf-down
uv run python experiment_runner.py -m anthropic/claude-opus-4.8  -s intf-down

# 3. evaluate by hand (press f / m / s for each answer)
uv run python evaluate_experiments.py

# 4. aggregate
uv run python analyze_experiments.py --scenario intf-down
```

---

## Where everything is written

| File | Written by | Contents |
|---|---|---|
| `experiment_log.jsonl` | `experiment_runner.py` | One row per turn: query, final `output`, `run_id`, tokens, cost, success |
| `command_failures.jsonl` | MCP servers | Invalid SR Linux commands (counted as `invalid_commands`) |
| `evaluation_log.jsonl` | `evaluate_experiments.py` | One manual verdict per session (`found_issue` + optional `note`), keyed by `session_id` |
| `evaluation_review.md` / `.csv` | `evaluate_experiments.py --review` | Read-only join of answers + your verdicts |
| `logs/matrix/<TS>/progress.txt` | `run_experiment_matrix.sh` | Completed `scenario\|model\|repeat` combos (read by `--resume`); per-run console logs sit next to it |
| `wall_clock_durations.jsonl` | `recover_wall_clock.py` | Real elapsed time per session (`wall_clock_s`, `inference_s`, `overhead_s`), keyed by `session_id` |
| `join_verification.jsonl` | `recover_wall_clock.py` | Per-session generation-ID proof that the wall-clock join is correct |
| `logfire_root_spans.jsonl` | `fetch_logfire_exports.py` | Checked-in Logfire export: root-span start/end + trace_id (the wall-clock measurement) |
| `logfire_generation_ids.jsonl` | `fetch_logfire_exports.py` | Checked-in Logfire export: generation-ID → trace lookup (the join-proof data) |
| `figures/*.pdf` (averaged) | `visualize_experiments.py` | Up to 23 vector charts averaged across runs (cost, time, tokens, found-rate, …) |
| `figures/raw_experiments.html` | `raw_experiments_html.py` | Sortable per-session table |
| `figures/raw_sessions_overview.pdf` | `visualize_raw_experiments.py` | One bar per session, verdict-coloured labels |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Run aborts immediately with a `setup` error | `setup.sh` exited non-zero. Test it by hand; check the container name (`clab-testlab-router1`) and that the lab is deployed. |
| `success=True` but answer is wrong | Expected — `success` only means "didn't crash." Correctness is your call in Step ③. |
| `success=False` with `error="DNF …"` | The run hit the `--max-seconds` wall-clock cap (600 s in the recorded matrix) — a timeout, not a crash. 55/300 recorded runs ended this way. |
| `analyze_experiments.py` shows no correctness columns | No verdicts yet, or `evaluation_log.jsonl` is missing. Run Step ③ first. |
| No `root_cause` reference shown while evaluating | The scenario has no `ground_truth.yaml` (or it lacks a `root_cause`). You can still label by hand; add one to get the reminder. |
| Costs show as unavailable | OpenRouter generation records weren't indexed yet; usually transient. Token counts still recorded. |
| Lab won't deploy / connection refused | Lab not running: `sudo containerlab deploy -t testlab.clab.yml`; check `sudo containerlab inspect --all`. |

---

## Tips for a clean comparison

- **Reset state between runs.** A flaky `teardown.sh` can leave the fault in place
  and contaminate the next run. When in doubt, `sudo containerlab redeploy -t testlab.clab.yml`.
- **Evaluate consistently.** Decide up front what counts as a "find" (e.g. must name
  the interface, not just "a routing problem") and apply it the same way every time —
  notes on borderline calls help you stay consistent and are worth keeping for the writeup.
- **`--multi-turn` exists for staged investigations**, but the recorded experiments
  never used it — every scenario is single-query. Either way you evaluate only the
  final answer of each run, so end multi-turn scenarios with a query that asks for
  the diagnosis.
- **Write a specific `root_cause`.** Name the device/interface and mention the obvious
  wrong answers, so when you're labelling you don't credit a hedged "maybe DNS, maybe
  the interface" reply.
- **One fault per scenario.** If you want to test two faults, make two scenario folders.
```