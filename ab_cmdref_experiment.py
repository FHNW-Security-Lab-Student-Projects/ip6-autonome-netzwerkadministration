#!/usr/bin/env python3
"""A/B experiment: does the SR Linux read-command reference reduce invalid commands?

Runs one scenario N times in each of two conditions and records the per-run
invalid-command count, broken down by error_type:

    OFF  NETWORK_AGENT_CMDREF unset  — no reference in the agent's system prompt
    ON   NETWORK_AGENT_CMDREF=1      — reference cheat-sheet injected statically

Each run is a fresh `experiment_runner.py` subprocess (so the MCP server picks up
the env var). The runner clears command_failures.jsonl at the start of every run,
so after the subprocess exits that file holds exactly this run's device failures —
we read it, tally by error_type, and snapshot the rows into ab_cmdref_results.jsonl.

The metric that actually tests the hypothesis is the `parsing_error` subset (CLI
syntax rejected by the device). jsonrpc_get_error (bad YANG path) is reported too
but a read-command cheat-sheet wouldn't be expected to fix those.

Usage:
    uv run python ab_cmdref_experiment.py --scenario client1-client3-communication -n 8
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FAILURE_LOG = ROOT / 'command_failures.jsonl'
TRANSPORT_LOG = ROOT / 'transport_failures.jsonl'
RESULTS_PATH = ROOT / 'ab_cmdref_results.jsonl'


def _read_jsonl(path: Path) -> list[dict]:
    """Return the records left in a JSONL log by the last run (empty if absent)."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _run_once(model: str, scenario: str, cmdref_on: bool, env_base: dict) -> dict:
    """Run the scenario once and return a result row (counts by error_type)."""
    import os
    env = dict(env_base)
    if cmdref_on:
        env['NETWORK_AGENT_CMDREF'] = '1'
    else:
        env.pop('NETWORK_AGENT_CMDREF', None)

    proc = subprocess.run(
        ['uv', 'run', 'python', 'experiment_runner.py',
         '--model', model, '--scenario', scenario],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    failures = _read_jsonl(FAILURE_LOG)
    transport = _read_jsonl(TRANSPORT_LOG)
    by_type = Counter(f.get('error_type', 'unknown') for f in failures)
    bad_commands = [
        {'error_type': f.get('error_type'), 'device': f.get('device'),
         'command': f.get('command'), 'error_text': (f.get('error_text') or '')[:200]}
        for f in failures
    ]
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'condition': 'ON' if cmdref_on else 'OFF',
        'model': model,
        'scenario': scenario,
        'total_invalid': len(failures),  # command_failures only — excludes transport
        'parsing_error': by_type.get('parsing_error', 0),
        'jsonrpc_get_error': by_type.get('jsonrpc_get_error', 0),
        'jsonrpc_error': by_type.get('jsonrpc_error', 0),
        'transport_errors': len(transport),  # contamination signal, NOT counted as invalid
        'returncode': proc.returncode,
        'bad_commands': bad_commands,
    }


def _summary(rows: list[dict], condition: str) -> dict:
    sub = [r for r in rows if r['condition'] == condition]
    n = len(sub)
    if n == 0:
        return {'condition': condition, 'n': 0}

    def avg(key):
        return sum(r[key] for r in sub) / n

    def counts(key):
        return [r[key] for r in sub]

    return {
        'condition': condition,
        'n': n,
        'parsing_error': {'mean': round(avg('parsing_error'), 2),
                          'total': sum(counts('parsing_error')),
                          'per_run': counts('parsing_error')},
        'total_invalid': {'mean': round(avg('total_invalid'), 2),
                          'total': sum(counts('total_invalid')),
                          'per_run': counts('total_invalid')},
        'jsonrpc_get_error': {'mean': round(avg('jsonrpc_get_error'), 2),
                              'total': sum(counts('jsonrpc_get_error'))},
        'transport_errors': {'total': sum(counts('transport_errors')),
                             'runs_affected': sum(1 for c in counts('transport_errors') if c)},
    }


def main() -> None:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario', required=True)
    ap.add_argument('--model', default='z-ai/glm-5')
    ap.add_argument('-n', '--runs', type=int, default=8, help='runs per condition')
    args = ap.parse_args()

    env_base = dict(os.environ)
    rows: list[dict] = []

    # Interleave OFF/ON across rounds so any topology drift hits both conditions
    # evenly, rather than all-OFF-then-all-ON.
    plan = []
    for i in range(args.runs):
        plan.append(False)  # OFF
        plan.append(True)   # ON

    with RESULTS_PATH.open('a') as out:
        for idx, cmdref_on in enumerate(plan, 1):
            cond = 'ON ' if cmdref_on else 'OFF'
            print(f'\n=== run {idx}/{len(plan)}  [{cond}]  {args.scenario} ===', flush=True)
            row = _run_once(args.model, args.scenario, cmdref_on, env_base)
            rows.append(row)
            out.write(json.dumps(row) + '\n')
            out.flush()
            warn = '  ⚠ TRANSPORT' if row['transport_errors'] else ''
            print(f'    invalid={row["total_invalid"]}  '
                  f'parsing_error={row["parsing_error"]}  '
                  f'get_error={row["jsonrpc_get_error"]}  '
                  f'transport={row["transport_errors"]}  '
                  f'rc={row["returncode"]}{warn}',
                  flush=True)

    print('\n' + '=' * 64)
    print(f'  A/B SUMMARY  |  scenario={args.scenario}  model={args.model}')
    print('=' * 64)
    for cond in ('OFF', 'ON'):
        s = _summary(rows, cond)
        print(json.dumps(s, indent=2))
    print('=' * 64)
    print(f'  Per-run rows appended to: {RESULTS_PATH.name}')


if __name__ == '__main__':
    main()
