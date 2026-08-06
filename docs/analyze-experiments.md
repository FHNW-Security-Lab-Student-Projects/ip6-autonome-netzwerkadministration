# Analyze Experiments

## Purpose

`analyze_experiments.py` reads up to three inputs and prints a formatted comparison report to the terminal. Run it manually after collecting experiment data to compare LLM performance across scenarios.

The three inputs, joined by `session_id`:

| Input | Contents | If missing |
|---|---|---|
| `experiment_log.jsonl` | Session records from `experiment_runner.py` | Required |
| `evaluation_log.jsonl` | Your manual verdicts (`found_issue`) | Correctness columns omitted |
| `wall_clock_durations.jsonl` | Real elapsed time from `recover_wall_clock.py` | Wall-clock columns omitted |

---

## Usage

```bash
# Full report — all scenarios and models
uv run python analyze_experiments.py

# Filter to one scenario
uv run python analyze_experiments.py --scenario intf-down

# Also export the tables to CSV files
uv run python analyze_experiments.py --csv report

# Use a different log file
uv run python analyze_experiments.py --file path/to/other.jsonl
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--file` / `-f` | `experiment_log.jsonl` | Path to the JSONL log file |
| `--scenario` / `-s` | *(all)* | Filter to a single scenario label |
| `--csv` / `-c` | *(none)* | Base name for CSV export (see below) |
| `--eval-file` / `-e` | `evaluation_log.jsonl` | Path to the manual-verdict log; missing file is fine, correctness columns are omitted |
| `--wall-clock-file` / `-w` | `wall_clock_durations.jsonl` | Path to the recovered wall-clock file; missing file is fine, wall-clock columns are omitted |

---

## Report sections

### Scenario summary

One row per `(scenario, model)` combination. Shows averages across all turns in that group.

```
── SCENARIO SUMMARY  (averages per model per scenario) ─────────────────────────

                                       turns  avg_duration_s  avg_input_tokens  ...  avg_wall_clock_s  evaluated_turns  found_rate  median_ttd_s
scenario  model
intf-down anthropic/claude-opus-4.8        5            83.5           57427.0  ...              87.5                5        1.00          85.2
          z-ai/glm-5.2                     5            41.3           25300.0  ...              73.1                5        0.60          70.4
```

| Column | Description |
|---|---|
| `turns` | Number of sessions (user queries) in this group |
| `avg_duration_s` | Average **summed LLM inference time** per turn (seconds) — the sum of `generation_time` over all LLM round-trips, not elapsed time. For elapsed time use `avg_wall_clock_s` |
| `avg_input_tokens` | Average input tokens per turn (all agents combined) |
| `avg_output_tokens` | Average output tokens per turn (all agents combined) |
| `avg_tool_calls` | Average number of MCP tool invocations per turn |
| `avg_llm_requests` | Average number of LLM API round-trips per turn |
| `avg_invalid_commands` | Average number of invalid SR Linux commands per turn (from `command_failures.jsonl` deltas) |
| `total_cost_usd` | Sum of estimated cost across all turns in the group |
| `success_rate` | Fraction of turns that completed without error (1.0 = 100%). DNF timeouts count as failures here |
| `avg_wall_clock_s` | Average real elapsed time per turn — only when `wall_clock_durations.jsonl` is joined in. DNF runs are right-censored at the 600 s cap, so averages that include them understate the true time |
| `evaluated_turns` | Number of turns with a manual verdict — only when `evaluation_log.jsonl` is joined in |
| `found_rate` | Fraction of *evaluated* turns where the agent found the issue |
| `median_ttd_s` | Median wall-clock time to a *correct* diagnosis, over runs that finished within the cap (DNF runs excluded). NaN = no correct completed run |

### Per-agent breakdown

One row per `(model, agent_name)` combination. Shows how tokens and time are distributed between the orchestrator and each sub-agent. This is the primary reason for using `experiment_runner.py` over the web UI — the orchestrator row is only available in runner data. In the recorded experiments the agents appearing here are `network_agent`, `syslog_agent`, `snapshot_agent`, and `orchestrator` (`avg_duration_s` again = summed inference time).

```
── PER-AGENT BREAKDOWN  (averages across all scenarios) ────────────────────────

                                          runs  avg_input_tokens  avg_output_tokens  avg_tool_calls  avg_llm_requests  avg_duration_s  avg_cost_usd
model                     agent_name
anthropic/claude-opus-4.8 network_agent     50           32531.0             1820.0             5.0               4.0            36.1      0.208155
                          orchestrator      50            8964.0             1492.0             3.0               3.0            22.9      0.082120
                          snapshot_agent    31            9120.0              640.0             2.0               2.0            11.8      0.061240
                          syslog_agent      48           15932.0             1908.0             4.0               2.0            24.4      0.127360
z-ai/glm-5.2              network_agent     50           24200.0             1380.0             3.0               2.0            18.2      0.014100
                          orchestrator      50            7100.0              960.0             1.0               2.0             9.1      0.004900
```

### Failures

Lists all sessions where `success=False`, with the error message. Useful for spotting timeouts or API errors that may have skewed the comparison. In the recorded 300 runs, 55 rows here are `DNF (wall-clock timeout after 600s)` — the matrix runner's cap, not crashes.

---

## CSV export

`--csv report` produces three files alongside the terminal output:

| File | Contents |
|---|---|
| `report_summary.csv` | Scenario summary table (indexed by scenario + model) |
| `report_agents.csv` | Per-agent breakdown (indexed by model + agent_name) |
| `report_failures.csv` | Failed sessions (only created if there are failures) |

These can be opened directly in Excel, Numbers, or a Jupyter notebook for further analysis.

---

## Typical workflow

```bash
# 1. Run the same scenario with multiple models (the scenario folder provides
#    queries.txt — no -f needed; run_experiment_matrix.sh automates the full sweep)
uv run python experiment_runner.py -m z-ai/glm-5.2               -s intf-down
uv run python experiment_runner.py -m anthropic/claude-opus-4.8  -s intf-down
uv run python experiment_runner.py -m openai/gpt-5.5             -s intf-down

# 2. Generate the comparison report
uv run python analyze_experiments.py --scenario intf-down

# 3. Optionally export to CSV for a spreadsheet
uv run python analyze_experiments.py --scenario intf-down --csv results/intf-down
```

---

## Dependencies

Requires `pandas`, which is included in the project dependencies (`pyproject.toml`). No additional setup needed beyond the standard `uv sync`.