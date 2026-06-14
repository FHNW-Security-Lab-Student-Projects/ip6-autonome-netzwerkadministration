#!/usr/bin/env python3
"""Manual correctness evaluation for experiment_log.jsonl.

There is no LLM judge. You read each session's final answer yourself and call it:
did the agent find the issue, yes or no? The script just makes that fast — it walks
you through the un-evaluated sessions one at a time, shows the query and the full
answer (plus the scenario's `root_cause` as a reference, if one exists), and records
your verdict to evaluation_log.jsonl. That file is keyed by session_id and is exactly
what analyze_experiments.py / raw_experiments_html.py join on, so your manual labels
flow straight into the reports.

Usage:
    # Walk through every session that doesn't have a verdict yet
    uv run python evaluate_experiments.py

    # Re-evaluate sessions even if they already have a verdict
    uv run python evaluate_experiments.py --redo

    # Only sessions from a specific date / model / scenario
    uv run python evaluate_experiments.py --since 2026-05-20
    uv run python evaluate_experiments.py --scenario bgp-router1-router2-down
    uv run python evaluate_experiments.py --model anthropic/claude-sonnet-4.6

    # Render a read-only report of the verdicts you've recorded (no prompts)
    uv run python evaluate_experiments.py --review        # → evaluation_review.md
    uv run python evaluate_experiments.py --review-csv    # → evaluation_review.csv

At each prompt:  [f]ound  [m]issed  [s]kip  [q]uit
You can append a free-text note after the letter, e.g. `f named the right interface`.
"""

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent
EXPERIMENT_LOG = REPO_ROOT / 'experiment_log.jsonl'
EVALUATION_LOG = REPO_ROOT / 'evaluation_log.jsonl'
REVIEW_MD = REPO_ROOT / 'evaluation_review.md'
REVIEW_CSV = REPO_ROOT / 'evaluation_review.csv'
SCENARIOS_DIR = REPO_ROOT / 'scenarios'


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with open(path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')


def _load_root_cause(scenario: str) -> str | None:
    """Return the scenario's root_cause text as a reference, or None if absent.

    Unlike the old LLM judge, this is purely a reminder shown to you while you
    evaluate — a session with no ground_truth.yaml can still be labelled by hand.
    """
    gt_path = SCENARIOS_DIR / scenario / 'ground_truth.yaml'
    if not gt_path.exists():
        return None
    data = yaml.safe_load(gt_path.read_text()) or {}
    root_cause = data.get('root_cause')
    if not root_cause or not str(root_cause).strip():
        return None
    return str(root_cause).strip()


def _filter_last_turn(sessions: list[dict]) -> list[dict]:
    """Keep only the chronologically last session per (model, scenario, run_id) group.

    The diagnosis is the final answer of a run — in a multi-turn investigation the
    intermediate turns are exploration, so only the concluding answer is evaluated.
    """
    by_group: dict[tuple[str, str, str], dict] = {}
    for s in sessions:
        key = (s.get('model', ''), s.get('scenario', ''), s.get('run_id', ''))
        existing = by_group.get(key)
        if existing is None or s.get('started_at', '') > existing.get('started_at', ''):
            by_group[key] = s
    return list(by_group.values())


def _select_sessions(
    sessions: list[dict],
    *,
    since: str | None,
    model_filter: str | None,
    scenario_filter: str | None,
) -> list[dict]:
    selected = sessions
    if since:
        selected = [s for s in selected if s.get('started_at', '') >= since]
    if model_filter:
        selected = [s for s in selected if s.get('model') == model_filter]
    if scenario_filter:
        selected = [s for s in selected if s.get('scenario') == scenario_filter]
    return selected


def _make_record(session: dict, found_issue: bool, note: str) -> dict:
    """Build one evaluation_log.jsonl row for a manual verdict."""
    return {
        'session_id': session['session_id'],
        'run_id': session.get('run_id', ''),
        'model': session.get('model', ''),
        'scenario': session.get('scenario', ''),
        'found_issue': found_issue,
        'note': note,
        # Token effort, carried over so verdicts can be weighed against cost/effort.
        # Native counts (from result.usage()) are the provider-tokenizer counts that
        # OpenRouter bills on — always valid, and the basis for cost. Normalized counts
        # (Generation API) are model-agnostic, so they make "effort" comparable across
        # models that tokenize differently, but are null/INVALID when a generation was
        # dropped (normalized_complete=False).
        'input_tokens': session.get('total_input_tokens', 0),
        'output_tokens': session.get('total_output_tokens', 0),
        'normalized_input_tokens': session.get('total_normalized_input_tokens'),
        'normalized_output_tokens': session.get('total_normalized_output_tokens'),
        'normalized_complete': session.get('normalized_complete', True),
        'total_cost_usd': session.get('total_cost_usd', 0.0),
        # True ⇒ the price was estimated from normalized tokens (native was invalid).
        'cost_estimated': session.get('cost_estimated', False),
        'evaluator': 'manual',
        'evaluated_at': datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Interactive evaluation
# ---------------------------------------------------------------------------

_RULE = '═' * 80
_THIN = '─' * 80


def _show_session(session: dict, root_cause: str | None, position: str) -> None:
    print(f'\n{_RULE}')
    print(f'  {position}  ·  {session.get("model", "?")}  ·  {session.get("scenario", "?")}')
    print(f'  session {session["session_id"][:8]}  ·  started {session.get("started_at", "?")}')
    print(_RULE)
    print(f'\nQUERY:\n  {session.get("user_query", "")}\n')
    print('AGENT ANSWER:')
    output = session.get('output') or ''
    if output.strip():
        for line in output.splitlines():
            print(f'  {line}')
    else:
        print('  (no final answer — agent produced empty output; '
              f'success={session.get("success")}, error={session.get("error", "")[:120]!r})')
    if root_cause:
        print(f'\n{_THIN}\nREFERENCE root_cause (what a correct diagnosis should identify):')
        for line in root_cause.splitlines():
            print(f'  {line}')
    print(_THIN)


def _parse_input(raw: str) -> tuple[str | None, str]:
    """Split a prompt response into (command, note). Empty/invalid → (None, '')."""
    raw = raw.strip()
    if not raw:
        return None, ''
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    note = parts[1].strip() if len(parts) > 1 else ''
    if cmd in ('f', 'found'):
        return 'found', note
    if cmd in ('m', 'missed', 'miss'):
        return 'missed', note
    if cmd in ('s', 'skip'):
        return 'skip', note
    if cmd in ('q', 'quit', 'exit'):
        return 'quit', note
    return None, ''


def _run_interactive(args: argparse.Namespace) -> None:
    sessions = _read_jsonl(EXPERIMENT_LOG)
    if not sessions:
        print(f'No sessions found in {EXPERIMENT_LOG}.', file=sys.stderr)
        return

    existing = {row['session_id']: row for row in _read_jsonl(EVALUATION_LOG)}

    selected = _select_sessions(
        sessions,
        since=args.since,
        model_filter=args.model,
        scenario_filter=args.scenario,
    )
    selected = _filter_last_turn(selected)
    selected.sort(key=lambda s: s.get('started_at', ''))

    if args.redo:
        todo = selected
    else:
        todo = [s for s in selected if s['session_id'] not in existing]

    if not todo:
        print(f'All {len(selected)} selected session(s) already evaluated. '
              f'Use --redo to re-evaluate.')
        return

    print(f'\n{len(todo)} session(s) to evaluate.  '
          'Commands: [f]ound  [m]issed  [s]kip  [q]uit  '
          '(append a note after the letter, e.g. "f right interface")')

    labelled = 0
    for i, session in enumerate(todo, 1):
        root_cause = _load_root_cause(session.get('scenario', ''))
        _show_session(session, root_cause, position=f'[{i}/{len(todo)}]')

        if not args.redo and session['session_id'] in existing:
            prev = existing[session['session_id']]
            prev_label = 'found' if prev.get('found_issue') else 'missed'
            print(f'  (already evaluated as {prev_label} — labelling again will overwrite it)')

        while True:
            cmd, note = _parse_input(input('  [f]ound / [m]issed / [s]kip / [q]uit > '))
            if cmd is None:
                print('  ? type f, m, s, or q.')
                continue
            break

        if cmd == 'quit':
            print('\nStopping early.')
            break
        if cmd == 'skip':
            print('  skipped (left un-evaluated).')
            continue

        found = cmd == 'found'
        existing[session['session_id']] = _make_record(session, found, note)
        _write_jsonl(EVALUATION_LOG, list(existing.values()))  # persist after each verdict
        labelled += 1
        print(f'  recorded: {"FOUND ✓" if found else "MISSED ✗"}'
              f'{f" — {note}" if note else ""}')

    _print_summary(existing, just_labelled=labelled)


def _print_summary(verdicts_by_id: dict[str, dict], just_labelled: int) -> None:
    print(f'\n{_RULE}')
    print(f'  Recorded {just_labelled} verdict(s) this session · '
          f'{len(verdicts_by_id)} total in {EVALUATION_LOG.name}')
    print(_RULE)
    by_model_scenario: dict[tuple[str, str], dict[str, int]] = {}
    for v in verdicts_by_id.values():
        key = (v.get('model', '?'), v.get('scenario', '?'))
        bucket = by_model_scenario.setdefault(key, {'found': 0, 'not_found': 0})
        bucket['found' if v.get('found_issue') else 'not_found'] += 1

    print(f'  {"model":<32}  {"scenario":<26}  {"found":>5}  {"missed":>6}  {"rate":>5}')
    print(f'  {"-" * 32}  {"-" * 26}  {"-" * 5}  {"-" * 6}  {"-" * 5}')
    for (model, scenario), b in sorted(by_model_scenario.items()):
        total = b['found'] + b['not_found']
        rate = b['found'] / total if total else 0.0
        print(f'  {model[:32]:<32}  {scenario[:26]:<26}  '
              f'{b["found"]:>5}  {b["not_found"]:>6}  {rate:>5.0%}')
    print(_RULE)
    print()


# ---------------------------------------------------------------------------
# Read-only reports (no prompts) — render the verdicts you've already recorded
# ---------------------------------------------------------------------------

def _quote_block(text: str, prefix: str = '> ') -> str:
    """Render multi-line text as a Markdown blockquote."""
    if not text:
        return f'{prefix}_(no answer)_'
    return '\n'.join(f'{prefix}{line}' if line else prefix.rstrip() for line in text.splitlines())


def _render_markdown(joined: list[tuple[dict, dict | None]]) -> str:
    """Render the side-by-side answer + verdict report as Markdown."""
    parts: list[str] = [
        '# Evaluation review\n',
        f'_Generated {datetime.now(timezone.utc).isoformat()} · {len(joined)} sessions._\n',
        '\n---\n',
    ]
    for session, verdict in joined:
        sid = session['session_id'][:8]
        model = session.get('model', '?')
        scenario = session.get('scenario', '?')
        parts.append(f'\n## {sid} · {model} · {scenario}\n')
        parts.append(f'\n**Query:** {session.get("user_query", "")}\n')
        parts.append(f'\n**Agent answer:**\n\n{_quote_block(session.get("output", ""))}\n')

        if verdict is None:
            parts.append('\n**Verdict:** _not yet evaluated — run `evaluate_experiments.py`._\n')
        else:
            label = 'FOUND ISSUE ✓' if verdict['found_issue'] else 'DID NOT FIND ISSUE ✗'
            note = verdict.get('note', '')
            parts.append(f'\n**Verdict:** {label}'
                         f'{f" — _{note}_" if note else ""}\n')
        parts.append('\n---\n')
    return ''.join(parts)


def _write_review_csv(joined: list[tuple[dict, dict | None]], path: Path) -> None:
    """Write a CSV joining each answer with its manual verdict."""
    fieldnames = [
        'session_id', 'run_id', 'model', 'scenario', 'started_at',
        'user_query', 'agent_output',
        'found_issue', 'note', 'evaluated_at',
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for session, verdict in joined:
            row = {
                'session_id': session['session_id'],
                'run_id': session.get('run_id', ''),
                'model': session.get('model', ''),
                'scenario': session.get('scenario', ''),
                'started_at': session.get('started_at', ''),
                'user_query': session.get('user_query', ''),
                'agent_output': session.get('output', ''),
            }
            if verdict is not None:
                row.update({
                    'found_issue': verdict['found_issue'],
                    'note': verdict.get('note', ''),
                    'evaluated_at': verdict.get('evaluated_at', ''),
                })
            writer.writerow(row)


def _do_review(args: argparse.Namespace) -> None:
    """Render evaluation_review.md / .csv from existing logs. No prompts."""
    sessions = _read_jsonl(EXPERIMENT_LOG)
    if not sessions:
        print(f'No sessions found in {EXPERIMENT_LOG}.', file=sys.stderr)
        return
    verdicts_by_id = {row['session_id']: row for row in _read_jsonl(EVALUATION_LOG)}

    selected = _select_sessions(
        sessions,
        since=args.since,
        model_filter=args.model,
        scenario_filter=args.scenario,
    )
    selected = _filter_last_turn(selected)
    selected.sort(key=lambda s: s.get('started_at', ''))

    if args.sample is not None and args.sample < len(selected):
        rng = random.Random(args.sample_seed)
        selected = rng.sample(selected, args.sample)
        selected.sort(key=lambda s: s.get('started_at', ''))

    joined = [(s, verdicts_by_id.get(s['session_id'])) for s in selected]

    if args.review:
        REVIEW_MD.write_text(_render_markdown(joined))
        print(f'Wrote {REVIEW_MD} ({len(joined)} sessions).')
    if args.review_csv:
        _write_review_csv(joined, REVIEW_CSV)
        print(f'Wrote {REVIEW_CSV} ({len(joined)} sessions).')

    missing = sum(1 for _, v in joined if v is None)
    if missing:
        print(f'  Note: {missing}/{len(joined)} sessions have no verdict yet. '
              f'Run `evaluate_experiments.py` to label them.',
              file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Manual correctness evaluation for experiment_log.jsonl.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--redo', action='store_true',
                        help='Re-evaluate sessions even if they already have a verdict.')
    parser.add_argument('--since', metavar='YYYY-MM-DD',
                        help='Only sessions whose started_at is on or after this date.')
    parser.add_argument('--model', metavar='MODEL_ID',
                        help='Only sessions run with this model under test.')
    parser.add_argument('--scenario', metavar='LABEL',
                        help='Only sessions for this scenario.')
    parser.add_argument('--review', action='store_true',
                        help=f'Render a read-only Markdown report to {REVIEW_MD.name} '
                             'instead of prompting. Joins answers with recorded verdicts.')
    parser.add_argument('--review-csv', action='store_true',
                        help=f'Render a CSV of answers + verdicts to {REVIEW_CSV.name}. '
                             'Can be combined with --review.')
    parser.add_argument('--sample', type=int, metavar='N',
                        help='Randomly sample N sessions before rendering a review report.')
    parser.add_argument('--sample-seed', type=int, default=0, metavar='SEED',
                        help='Seed for --sample so repeated reports stay reproducible (default: 0).')
    args = parser.parse_args()

    if args.review or args.review_csv:
        _do_review(args)
        return

    _run_interactive(args)


if __name__ == '__main__':
    main()
