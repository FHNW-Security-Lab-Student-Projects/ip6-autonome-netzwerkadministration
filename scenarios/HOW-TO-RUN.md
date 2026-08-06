# How to run a scenario with `experiment_runner`

Quickstart for launching the multi-agent troubleshooter against one fault-injection
scenario in this folder. For the catalog of scenarios and difficulty tiers see
[scenario-guide.md](scenario-guide.md); for the full reference (flags, logging, evaluation) see
[../docs/running-experiments.md](../docs/running-experiments.md).

## TL;DR

```bash
uv run python experiment_runner.py --model anthropic/claude-opus-4.8 --scenario intf-down
```

Run it from the repo root, inside the **devcontainer** where the lab lives (the runner
calls `docker exec` via each scenario's `setup.sh` / `teardown.sh`). `--scenario` is the
folder name under `scenarios/`.

## What the runner does for a scenario

When `--scenario <name>` matches a folder in `scenarios/`, the runner automatically:

1. Loads the queries from `scenarios/<name>/queries.txt` (one per line; `#` lines ignored).
2. Runs `scenarios/<name>/setup.sh` to **inject the fault** (aborts if setup fails).
3. Runs every query through the orchestrator + sub-agents, recording tokens / cost / latency.
4. Runs `scenarios/<name>/teardown.sh` to **restore the topology** (best-effort, always attempted).
5. Fetches real costs from OpenRouter and appends each turn to `experiment_log.jsonl`.

## Common variations

```bash
# Pick a different model (any OpenRouter model ID)
uv run python experiment_runner.py --model z-ai/glm-5.2 --scenario missing-vlan-on-trunk

# Multi-turn: carry conversation history between the queries in queries.txt
# (unused in the recorded experiments — all scenarios are single-query)
uv run python experiment_runner.py --model anthropic/claude-opus-4.8 --scenario mtu-blackhole --multi-turn

# Sanity run against the UNBROKEN topology (skip setup.sh / teardown.sh)
uv run python experiment_runner.py --model anthropic/claude-opus-4.8 --scenario intf-down --no-fault

# Ad-hoc queries from a file outside scenarios/ (no fault injection)
uv run python experiment_runner.py --model z-ai/glm-5.2 --scenario adhoc --file path/to/queries.txt
```

| Flag | Purpose |
|---|---|
| `--model` / `-m` | OpenRouter model ID (default: `DEFAULT_AGENT_MODEL` in `client_agent.py`, currently `z-ai/glm-5`) |
| `--scenario` / `-s` | Scenario folder name under `scenarios/` (falls back to `$EXPERIMENT_SCENARIO`) |
| `--file` / `-f` | Plain-text query file (only used when `--scenario` is not a real folder) |
| `--multi-turn` | Keep conversation history across the queries in one run |
| `--no-fault` | Skip `setup.sh` / `teardown.sh` (baseline sanity check) |

## Batch runs: `run_experiment_matrix.sh`

The recorded experiments were driven by [`../run_experiment_matrix.sh`](../run_experiment_matrix.sh),
which sweeps the full matrix (6 models × 10 scenarios × 5 repeats = 300 runs) by invoking
`experiment_runner.py` per combo. Flags: `--models a/b,c/d`, `--scenarios x,y`, `--repeats N`,
`--dry-run` (print the plan, run nothing), `--resume` (skip combos already done). Each run has a
600 s wall-clock cap (`MAX_SECONDS`) — on expiry it is logged as a DNF (`success=false`). Progress
and per-run logs go to `logs/matrix/<TIMESTAMP>/`; its `progress.txt` is what `--resume` reads.

## After the run

Evaluate correctness by hand (no LLM judge — you read each answer and press f/m/s):

```bash
uv run python evaluate_experiments.py
uv run python evaluate_experiments.py --review   # → evaluation_review.md (read-only report)
```

Other flags: `--review-csv` (CSV report, combinable with `--review`), `--redo` (re-evaluate
sessions that already have a verdict), `--since YYYY-MM-DD` / `--model` / `--scenario`
(filter which sessions are shown), `--sample N` (random subset for a review report,
seeded via `--sample-seed`).

> **Reset between runs:** if a teardown leaves the lab in a bad state, or BGP hasn't
> reconverged, redeploy the lab:
> `sudo containerlab redeploy --cleanup -t testlab.clab.yml`
