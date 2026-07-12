#!/usr/bin/env python3
"""Recover per-session wall-clock durations from Logfire traces.

The experiment log's duration_s is the sum of OpenRouter generation_time over
all LLM round-trips (see experiment_tracker.py), i.e. pure inference time. It
excludes tool execution, MCP round-trips, agent-delegation overhead, and any
client-side waiting — so it is a lower bound on the time a run actually took,
and cannot be presented as "troubleshooting time".

The true wall-clock time is recoverable without rerunning anything: every run
was traced to Logfire (logfire.instrument_pydantic_ai in client_agent.py), and
each session produced exactly one root 'orchestrator run' span whose
start/end timestamps bracket the whole investigation (sub-agents run in-process
inside it; the topology/state-snapshot refreshes run concurrently with its
start). This script queries those root spans via the Logfire query API and
joins them to experiment_log.jsonl by timestamp: the root span starts ~0.1 s
after the session's started_at, and sessions are minutes apart, so the nearest-
match join is unambiguous. Logfire scrubs session_id from its own records,
which is why the join is by time and not by ID.

Per session it writes:
    wall_clock_s   — root-span end minus the session's started_at
    inference_s    — the log's duration_s (summed generation_time), for contrast
    overhead_s     — wall_clock_s - inference_s

Note: runs cancelled by the matrix runner's MAX_SECONDS=600 cap (see
run_experiment_matrix.sh) have wall_clock_s right-censored at ~600 s.

The timestamp join is additionally VERIFIED with OpenRouter generation IDs,
which are globally unique: every session's first_generation_id and
last_generation_id (recorded in experiment_log.jsonl from the X-Generation-Id
response headers) is looked up in Logfire (span attribute gen_ai.response.id)
and must land in exactly the trace the timestamp join picked. Two match
classes are expected:
    exact     — first ID is the trace's 1st generation and last ID its final
                one (all completed runs).
    contained — both IDs are inside the trace but not at its edges. This is
                the signature of DNF runs: on the 600 s timeout the
                orchestrator's record_agent_run never executes (see
                experiment_runner._run_turn), so its generation IDs are
                dropped from the log while remaining in the trace — the log's
                first/last IDs then come from completed sub-agent runs only.
Anything else (an ID found in a different trace, or missing from Logfire)
marks the session as NOT verified and is reported loudly.

The per-session proof is written to join_verification.jsonl, and each
wall_clock_durations.jsonl record carries gen_id_match / join_verified.

This script reads ONLY local files. The two Logfire exports it needs —
logfire_root_spans.jsonl (one ["start","end","trace_id"] per line) and
logfire_generation_ids.jsonl (one [gen_id, trace_id, pos, n_gens] per line,
where pos is the ID's 1-based chronological rank among the trace's distinct
generations) — are produced by fetch_logfire_exports.py from the Logfire
query API and checked in, since Logfire retention expires.

Usage:
    uv run python recover_wall_clock.py                  # writes wall_clock_durations.jsonl + join_verification.jsonl
    uv run python recover_wall_clock.py --dry-run        # summary only, no files
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
EXPERIMENT_LOG = HERE / 'experiment_log.jsonl'
SPANS_EXPORT = HERE / 'logfire_root_spans.jsonl'
GEN_IDS_EXPORT = HERE / 'logfire_generation_ids.jsonl'
OUTPUT = HERE / 'wall_clock_durations.jsonl'
VERIFICATION_OUTPUT = HERE / 'join_verification.jsonl'

# The root span starts just after finish_session's started_at (~0.1 s observed).
# A session whose nearest root span starts outside this window stays unmatched.
JOIN_WINDOW_S = 10.0


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def load_spans_export(path: Path) -> list[dict]:
    spans = []
    with open(path) as f:
        for line in f:
            start, end, trace_id = json.loads(line)
            spans.append({
                'start': parse_ts(start), 'end': parse_ts(end),
                'duration': (parse_ts(end) - parse_ts(start)).total_seconds(),
                'trace_id': trace_id,
            })
    spans.sort(key=lambda sp: sp['start'])
    return spans


def load_gen_ids_export(path: Path) -> dict[str, dict]:
    """Load [gen_id, trace_id, pos, n_gens] lines into a lookup by gen_id."""
    ranks = {}
    with open(path) as f:
        for line in f:
            gen_id, trace_id, pos, n_gens = json.loads(line)
            ranks[gen_id] = {'trace_id': trace_id, 'pos': pos, 'n_gens': n_gens}
    return ranks


def verify_join(matched: list[dict], sessions: list[dict], ranks: dict[str, dict]) -> list[dict]:
    """Cross-check the timestamp join against OpenRouter generation IDs.

    For each matched session, both of its recorded generation IDs must resolve to
    the timestamp-joined trace. Annotates each matched record with
    gen_id_match / join_verified and returns one proof record per session.
    """
    by_id = {s['session_id']: s for s in sessions}
    proofs = []
    for m in matched:
        sess = by_id[m['session_id']]
        first, last = sess.get('first_generation_id', ''), sess.get('last_generation_id', '')
        rf, rl = ranks.get(first), ranks.get(last)
        if rf is None or rl is None:
            match = 'missing-id'
        elif rf['trace_id'] != m['trace_id'] or rl['trace_id'] != m['trace_id']:
            match = 'wrong-trace'
        elif rf['pos'] == 1 and rl['pos'] == rl['n_gens']:
            match = 'exact'
        else:
            match = 'contained'
        m['gen_id_match'] = match
        m['join_verified'] = match in ('exact', 'contained')
        proofs.append({
            'session_id': m['session_id'],
            'run_id': m['run_id'],
            'scenario': m['scenario'],
            'model': m['model'],
            'trace_id': m['trace_id'],
            'span_start_delta_s': m['span_start_delta_s'],
            'first_generation_id': first,
            'first_id_trace': rf['trace_id'] if rf else None,
            'first_id_pos': rf['pos'] if rf else None,
            'last_generation_id': last,
            'last_id_trace': rl['trace_id'] if rl else None,
            'last_id_pos': rl['pos'] if rl else None,
            'trace_generations': rl['n_gens'] if rl else None,
            'match': match,
            'verified': m['join_verified'],
        })
    return proofs


def join(sessions: list[dict], spans: list[dict]) -> tuple[list[dict], list[dict]]:
    """Match each session to the nearest unused root span within JOIN_WINDOW_S."""
    matched, unmatched = [], []
    used: set[int] = set()
    for sess in sorted(sessions, key=lambda s: s['started_at']):
        t0 = parse_ts(sess['started_at'])
        best, best_delta = None, None
        for i, sp in enumerate(spans):
            if i in used:
                continue
            delta = (sp['start'] - t0).total_seconds()
            if abs(delta) <= JOIN_WINDOW_S and (best_delta is None or abs(delta) < abs(best_delta)):
                best, best_delta = i, delta
        if best is None:
            unmatched.append(sess)
            continue
        used.add(best)
        sp = spans[best]
        wall = round((sp['end'] - t0).total_seconds(), 3)
        matched.append({
            'session_id': sess['session_id'],
            'run_id': sess['run_id'],
            'scenario': sess['scenario'],
            'model': sess['model'],
            'success': sess.get('success'),
            'error': sess.get('error') or '',
            'inference_s': sess['duration_s'],
            'wall_clock_s': wall,
            'span_duration_s': round(sp['duration'], 3),
            'overhead_s': round(wall - sess['duration_s'], 3),
            'span_start_delta_s': round(best_delta, 3),
            'trace_id': sp['trace_id'],
        })
    return matched, unmatched


def summarize(matched: list[dict], unmatched: list[dict], n_spans: int) -> None:
    print(f'sessions matched: {len(matched)}  unmatched: {len(unmatched)}  '
          f'(root spans available: {n_spans})')
    for sess in unmatched:
        print(f'  UNMATCHED {sess["session_id"]}  {sess["started_at"]}  {sess["scenario"]}')
    capped = [m for m in matched if m['wall_clock_s'] >= 595]
    print(f'runs at the 600 s matrix cap (right-censored): {len(capped)}')

    counts: dict[str, int] = {}
    for m in matched:
        counts[m.get('gen_id_match', 'unverified')] = counts.get(m.get('gen_id_match', 'unverified'), 0) + 1
    print('generation-ID verification: '
          + '  '.join(f'{k}: {v}' for k, v in sorted(counts.items())))
    bad = [m for m in matched if m.get('join_verified') is False]
    for m in bad:
        print(f'  NOT VERIFIED {m["session_id"]}  trace {m["trace_id"]}  ({m["gen_id_match"]})')

    by_model: dict[str, list[dict]] = {}
    for m in matched:
        by_model.setdefault(m['model'], []).append(m)
    print(f'\n{"model":<34} {"n":>4} {"med wall":>9} {"med infer":>10} '
          f'{"med ovh":>8} {"infer/wall":>10}')
    for model, runs in sorted(by_model.items()):
        med_wall = statistics.median(r['wall_clock_s'] for r in runs)
        med_inf = statistics.median(r['inference_s'] for r in runs)
        med_ovh = statistics.median(r['overhead_s'] for r in runs)
        ratios = [r['inference_s'] / r['wall_clock_s'] for r in runs if r['wall_clock_s'] > 0]
        print(f'{model:<34} {len(runs):>4} {med_wall:>8.1f}s {med_inf:>9.1f}s '
              f'{med_ovh:>7.1f}s {statistics.median(ratios):>9.1%}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--file', type=Path, default=EXPERIMENT_LOG, help='experiment log to join')
    parser.add_argument('--spans', type=Path, default=SPANS_EXPORT, help='local span export to use')
    parser.add_argument('--gen-ids', type=Path, default=GEN_IDS_EXPORT,
                        help='local generation-ID export to verify the join with')
    parser.add_argument('--out', type=Path, default=OUTPUT, help='output jsonl path')
    parser.add_argument('--verification-out', type=Path, default=VERIFICATION_OUTPUT,
                        help='per-session join-proof jsonl path')
    parser.add_argument('--dry-run', action='store_true', help='print summary only, write nothing')
    args = parser.parse_args()

    sessions = [json.loads(line) for line in args.file.open()]
    for path in (args.spans, args.gen_ids):
        if not path.exists():
            raise SystemExit(f'{path} not found — generate it with: '
                             'uv run python fetch_logfire_exports.py')
    spans = load_spans_export(args.spans)
    ranks = load_gen_ids_export(args.gen_ids)
    matched, unmatched = join(sessions, spans)
    proofs = verify_join(matched, sessions, ranks)
    summarize(matched, unmatched, len(spans))
    not_verified = [p for p in proofs if not p['verified']]
    if not_verified:
        raise SystemExit(f'{len(not_verified)} sessions failed generation-ID verification — '
                         'the timestamp join cannot be trusted for them; nothing written.')

    if not args.dry_run:
        with open(args.out, 'w') as f:
            for m in matched:
                f.write(json.dumps(m) + '\n')
        print(f'\nwrote {len(matched)} records to {args.out}')
        with open(args.verification_out, 'w') as f:
            for p in proofs:
                f.write(json.dumps(p) + '\n')
        print(f'wrote {len(proofs)} join proofs to {args.verification_out}')


if __name__ == '__main__':
    main()
