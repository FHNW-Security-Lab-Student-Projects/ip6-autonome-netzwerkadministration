#!/usr/bin/env bash
#
# run_experiment_matrix.sh — batch-run experiment_runner.py over a
# model × scenario matrix so you don't invoke each combo by hand.
#
# For every (scenario, model, repeat) it:
#   1. redeploys the containerlab topology for a clean lab (toggle below),
#   2. calls experiment_runner.py, which runs the scenario's setup.sh, sends the
#      query through the full agent pipeline, runs teardown.sh, and appends one
#      row per turn to experiment_log.jsonl.
#
# This automates Step ② of docs/running-experiments.md only. Evaluate
# (evaluate_experiments.py) and aggregate (analyze_experiments.py) as usual
# afterwards.
#
# NOTE on cost/time: the default matrix is 6 models × 10 scenarios × 5 repeats
# = 300 runs, each at `effort: high` reasoning (model_config.py). Opus / GPT-tier
# runs dominate cost. Trim MODELS / SCENARIOS / REPEATS, or run with --dry-run
# first to see exactly what would execute.
#
# Usage:
#   ./run_experiment_matrix.sh [--models a/b,c/d] [--scenarios x,y]
#                              [--repeats N] [--no-redeploy] [--resume [DIR]] [--dry-run]
#
# Resilience: completed runs are appended to experiment_log.jsonl immediately, and
# each finished combo is recorded in <logdir>/progress.txt. If the sweep is
# interrupted (Ctrl-C) or some runs fail, re-run with --resume to skip the combos
# already done and pick up where it left off — no duplicate rows, no re-doing work.
# Ctrl-C stops cleanly after the current run and prints the summary + resume hint.
#
# Note: no `set -e` — one failing combo must never abort the whole sweep.
set -uo pipefail

# ============================ CONFIG (edit me) ============================

# OpenRouter model IDs — 2 per price tier (see docs/model-tier-definition.md).
# HISTORY: on 2026-07-06 two failed qwen/qwen3.7-max runs (one JSON parse error,
# one upstream 429 rate limit before the first LLM request; both removed from
# experiment_log.jsonl) were re-run with a temporarily trimmed matrix
# (REPEATS=1, scenarios duplicate-ip-arp + one-way-route-filter). The full
# matrix below is the configuration of record for the 300 logged runs.

MODELS=(
  "anthropic/claude-opus-4.8"     "openai/gpt-5.5"                 # high
  "z-ai/glm-5.2"                  "qwen/qwen3.7-max"               # mid
  "deepseek/deepseek-v3.2"        "mistralai/ministral-14b-2512"   # low
)

# Scenario folder names under scenarios/.
SCENARIOS=(
   acl-silent-drop
   client3-port-down
   duplicate-ip-arp
   intf-down
   missing-export-policy
   missing-vlan-on-trunk
   mtu-blackhole
   one-way-route-filter
   switch1-uplink-down
   basic-client-communication
)

REPEATS=5                              # runs per (model, scenario) pair
MAX_SECONDS=600                          # wall-clock cap per run; 0 = no cap. On expiry the run
                                       # is logged as DNF (completed sub-agent stats are kept)
REDEPLOY_BETWEEN_RUNS=1                # 1 = redeploy lab before each run, 0 = don't
SETTLE_SECONDS=45                      # wait for SR Linux nodes to boot after redeploy
CLAB_TOPOLOGY="testlab.clab.yml"
REDEPLOY_CMD="containerlab redeploy --cleanup -t $CLAB_TOPOLOGY"   # no sudo
RUNNER_CMD="uv run experiment_runner.py"   # change if running via devcontainer
EXTRA_RUNNER_ARGS=""                   # e.g. "--multi-turn" (unused: scenarios are single-query)

# =========================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
RESUME=0
RESUME_DIR=""
INTERRUPTED=0

usage() {
  cat <<'EOF'
Usage: ./run_experiment_matrix.sh [options]

  --models  "a/b,c/d"   comma-separated model IDs (overrides MODELS)
  --scenarios "x,y"     comma-separated scenario folder names (overrides SCENARIOS)
  --repeats N           runs per (model, scenario) pair (overrides REPEATS)
  --max-seconds N       wall-clock cap per run; on expiry the run is logged as DNF
                        (overrides MAX_SECONDS; 0 = no cap)
  --no-redeploy         skip containerlab redeploy; rely on teardown.sh between runs
  --resume [DIR]        reuse a prior run's log dir (default: the most recent under
                        logs/matrix/) and skip combos already in its progress.txt
  --dry-run             print the planned commands and run nothing
  -h, --help            show this help

Edit the CONFIG block at the top of the script to change the defaults.
EOF
}

# --- CLI overrides ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --models)     IFS=',' read -r -a MODELS    <<< "$2"; shift 2 ;;
    --scenarios)  IFS=',' read -r -a SCENARIOS <<< "$2"; shift 2 ;;
    --repeats)    REPEATS="$2";                          shift 2 ;;
    --max-seconds) MAX_SECONDS="$2";                     shift 2 ;;
    --no-redeploy) REDEPLOY_BETWEEN_RUNS=0;              shift   ;;
    --resume)
      RESUME=1
      if [[ ${2:-} != "" && ${2:-} != -* ]]; then RESUME_DIR="$2"; shift 2; else shift; fi
      ;;
    --dry-run)    DRY_RUN=1;                             shift   ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

cd "$SCRIPT_DIR" || { echo "Cannot cd to $SCRIPT_DIR" >&2; exit 1; }

if [[ ${#MODELS[@]} -eq 0 || ${#SCENARIOS[@]} -eq 0 || "$REPEATS" -lt 1 ]]; then
  echo "Nothing to run: need at least one model, one scenario, and REPEATS >= 1." >&2
  exit 2
fi

TOTAL=$(( ${#SCENARIOS[@]} * ${#MODELS[@]} * REPEATS ))

# Fold the wall-clock cap into the runner args (0 = no cap). experiment_runner.py
# records a timed-out run as DNF and still logs the completed sub-agents' stats.
if [[ "$MAX_SECONDS" -gt 0 ]]; then
  EXTRA_RUNNER_ARGS="$EXTRA_RUNNER_ARGS --max-seconds $MAX_SECONDS"
fi

# Resolve the log dir: a fresh timestamped dir, or a prior one when resuming.
if [[ $RESUME -eq 1 ]]; then
  if [[ -n "$RESUME_DIR" ]]; then
    LOGDIR="${RESUME_DIR%/}"
  else
    LOGDIR="$(ls -d logs/matrix/*/ 2>/dev/null | sort | tail -1)"
    LOGDIR="${LOGDIR%/}"
  fi
  if [[ -z "$LOGDIR" || ! -d "$LOGDIR" ]]; then
    echo "Cannot resume: no prior run dir found under logs/matrix/ (or --resume DIR is invalid)." >&2
    exit 2
  fi
else
  TS="$(date +%Y%m%d-%H%M%S)"
  LOGDIR="logs/matrix/$TS"
fi
PROGRESS="$LOGDIR/progress.txt"
[[ $DRY_RUN -eq 1 ]] || mkdir -p "$LOGDIR"

# Combos already completed (only OK runs are recorded, so failed/interrupted
# combos are retried on --resume). Key: "scenario|model|r".
declare -A DONE=()
if [[ -f "$PROGRESS" ]]; then
  while IFS= read -r _k; do [[ -n "$_k" ]] && DONE["$_k"]=1; done < "$PROGRESS"
fi

echo "================================================================"
echo "  Experiment matrix"
echo "  Models:    ${#MODELS[@]}   Scenarios: ${#SCENARIOS[@]}   Repeats: $REPEATS"
echo "  Total runs: $TOTAL"
echo "  Redeploy between runs: $([[ $REDEPLOY_BETWEEN_RUNS -eq 1 ]] && echo yes || echo no)"
[[ "$MAX_SECONDS" -gt 0 ]] && echo "  Max seconds/run: $MAX_SECONDS (DNF on expiry)"
[[ $RESUME -eq 1 ]] && echo "  Resuming: ${#DONE[@]} combo(s) already done — will be skipped"
echo "  Logs:      $LOGDIR/"
[[ $DRY_RUN -eq 1 ]] && echo "  *** DRY RUN — nothing will execute ***"
echo "================================================================"

declare -a FAILURES=()
# Stop after the current run when interrupted (Ctrl-C), instead of dying mid-loop.
trap 'INTERRUPTED=1; echo; echo "  ⚠ interrupt received — stopping after the current run."' INT
START=$(date +%s)
idx=0
ok=0
fail=0
skipped=0

for scenario in "${SCENARIOS[@]}"; do
  for model in "${MODELS[@]}"; do
    for ((r = 1; r <= REPEATS; r++)); do
      idx=$((idx + 1))
      key="$scenario|$model|$r"
      slug="${model//\//__}"
      logfile="$LOGDIR/${scenario}__${slug}__r${r}.log"
      run_cmd="$RUNNER_CMD -m \"$model\" -s \"$scenario\" $EXTRA_RUNNER_ARGS"

      if [[ -n "${DONE[$key]:-}" ]]; then
        echo "  [$idx/$TOTAL]  $scenario | $model | run $r — already done, skipping"
        skipped=$((skipped + 1))
        continue
      fi

      echo
      echo "----------------------------------------------------------------"
      echo "  [$idx/$TOTAL]  $scenario  |  $model  |  run $r/$REPEATS"
      echo "----------------------------------------------------------------"

      if [[ $DRY_RUN -eq 1 ]]; then
        [[ $REDEPLOY_BETWEEN_RUNS -eq 1 ]] && echo "  + $REDEPLOY_CMD && sleep $SETTLE_SECONDS"
        echo "  + $run_cmd  2>&1 | tee $logfile"
        continue
      fi

      [[ $INTERRUPTED -eq 1 ]] && break 3

      if [[ $REDEPLOY_BETWEEN_RUNS -eq 1 ]]; then
        echo "  redeploying lab: $REDEPLOY_CMD"
        if ! $REDEPLOY_CMD; then
          echo "  ⚠ redeploy failed — skipping this run" | tee "$logfile"
          FAILURES+=("$scenario | $model | run $r (redeploy failed) -> $logfile")
          fail=$((fail + 1))
          continue
        fi
        sleep "$SETTLE_SECONDS"
      fi

      [[ $INTERRUPTED -eq 1 ]] && break 3

      $RUNNER_CMD -m "$model" -s "$scenario" $EXTRA_RUNNER_ARGS 2>&1 | tee "$logfile"
      rc=${PIPESTATUS[0]}
      if [[ $rc -eq 0 ]]; then
        ok=$((ok + 1))
        echo "$key" >> "$PROGRESS"     # mark done so --resume skips it next time
      else
        fail=$((fail + 1))
        FAILURES+=("$scenario | $model | run $r (exit $rc) -> $logfile")
        echo "  ⚠ run failed (exit $rc) — continuing" >&2
      fi

      [[ $INTERRUPTED -eq 1 ]] && break 3
    done
  done
done

ELAPSED=$(( $(date +%s) - START ))

echo
remaining=$(( TOTAL - ok - skipped ))

echo "================================================================"
echo "  SUMMARY"
echo "  Total: $TOTAL   OK: $ok   FAIL: $fail   Skipped: $skipped   Elapsed: ${ELAPSED}s"
echo "  Logs:  $LOGDIR/"
[[ $INTERRUPTED -eq 1 ]] && echo "  Interrupted before completing the matrix."
if [[ ${#FAILURES[@]} -gt 0 ]]; then
  echo "  ----------------------------------------------------------------"
  echo "  Failed runs:"
  for f in "${FAILURES[@]}"; do
    echo "    - $f"
  done
fi
if [[ $DRY_RUN -ne 1 && ( $INTERRUPTED -eq 1 || $fail -gt 0 || $remaining -gt 0 ) ]]; then
  echo "  ----------------------------------------------------------------"
  echo "  Resume the rest with:"
  echo "    ./run_experiment_matrix.sh --resume $LOGDIR"
fi
echo "================================================================"
[[ $DRY_RUN -eq 1 ]] && exit 0

# Next: evaluate and aggregate (docs/running-experiments.md)
if [[ $fail -eq 0 && $INTERRUPTED -eq 0 && $remaining -eq 0 ]]; then
  echo "Next: uv run python evaluate_experiments.py  →  uv run python analyze_experiments.py"
fi
exit $(( (fail > 0 || INTERRUPTED == 1) ? 1 : 0 ))
