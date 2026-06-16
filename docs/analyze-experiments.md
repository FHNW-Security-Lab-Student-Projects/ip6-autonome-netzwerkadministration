# Analyze Experiments

## Purpose

`analyze_experiments.py` reads `experiment_log.jsonl` and prints a formatted comparison report to the terminal. Run it manually after collecting experiment data to compare LLM performance across scenarios.

---

## Usage

```bash
# Full report — all scenarios and models
uv run python analyze_experiments.py

# Filter to one scenario
uv run python analyze_experiments.py --scenario bgp-01

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

---

## Report sections

### Scenario summary

One row per `(scenario, model)` combination. Shows averages across all turns in that group.

```
── SCENARIO SUMMARY  (averages per model per scenario) ─────────────────────────

                                      turns  avg_duration_s  avg_input_tokens  avg_output_tokens  avg_tool_calls  avg_llm_requests  total_cost_usd  success_rate
scenario model
bgp-01   anthropic/claude-sonnet-4.6      3            14.8            6025.0              595.0             6.0               5.0        0.000195           1.0
         z-ai/glm-5                       3            11.3            5300.0              540.0             4.0               4.0        0.000000           1.0
```

| Column | Description |
|---|---|
| `turns` | Number of sessions (user queries) in this group |
| `avg_duration_s` | Average wall-clock time per turn (seconds) |
| `avg_input_tokens` | Average input tokens per turn (all agents combined) |
| `avg_output_tokens` | Average output tokens per turn (all agents combined) |
| `avg_tool_calls` | Average number of MCP tool invocations per turn |
| `avg_llm_requests` | Average number of LLM API round-trips per turn |
| `total_cost_usd` | Sum of estimated cost across all turns in the group |
| `success_rate` | Fraction of turns that completed without error (1.0 = 100%) |

### Per-agent breakdown

One row per `(model, agent_name)` combination. Shows how tokens and time are distributed between the orchestrator and each sub-agent. This is the primary reason for using `experiment_runner.py` over the web UI — the orchestrator row is only available in runner data.

```
── PER-AGENT BREAKDOWN  (averages across all scenarios) ────────────────────────

                                           runs  avg_input_tokens  avg_output_tokens  avg_tool_calls  avg_llm_requests  avg_duration_s  avg_cost_usd
model                       agent_name
anthropic/claude-sonnet-4.6 network_agent     3            4821.0              412.0             4.0               3.0             9.2      0.000062
                            orchestrator      3            1204.0              183.0             2.0               2.0             5.6      0.000003
z-ai/glm-5                  network_agent     3            4200.0              380.0             3.0               2.0             8.2      0.000000
                            orchestrator      3            1100.0              160.0             1.0               2.0             3.1      0.000000
```

### Failures

Lists all sessions where `success=False`, with the error message. Useful for spotting timeouts or API errors that may have skewed the comparison.

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
# 1. Run the same scenario with multiple models
uv run python experiment_runner.py -m z-ai/glm-5                  -s bgp-01 -f path/to/queries.txt
uv run python experiment_runner.py -m anthropic/claude-sonnet-4.6 -s bgp-01 -f path/to/queries.txt
uv run python experiment_runner.py -m google/gemini-2.0-flash-001 -s bgp-01 -f path/to/queries.txt

# 2. Generate the comparison report
uv run python analyze_experiments.py --scenario bgp-01

# 3. Optionally export to CSV for a spreadsheet
uv run python analyze_experiments.py --scenario bgp-01 --csv results/bgp-01
```

---

## Dependencies

Requires `pandas`, which is included in the project dependencies (`pyproject.toml`). No additional setup needed beyond the standard `uv sync`.