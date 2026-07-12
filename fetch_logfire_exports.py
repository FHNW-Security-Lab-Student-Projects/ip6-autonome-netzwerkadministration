#!/usr/bin/env python3
"""Fetch the Logfire exports that recover_wall_clock.py reads.

Queries the Logfire HTTP query API (/v1/query) and writes two files:

    logfire_root_spans.jsonl      — one ["start","end","trace_id"] per root
        'orchestrator run' span. These timestamps are the source of the
        wall-clock durations; recover_wall_clock.py joins them to
        experiment_log.jsonl by started_at.
    logfire_generation_ids.jsonl  — one [gen_id, trace_id, pos, n_gens] per
        first/last OpenRouter generation ID recorded in experiment_log.jsonl.
        pos is the ID's 1-based chronological rank among the trace's distinct
        generations. recover_wall_clock.py uses this to verify the timestamp
        join: both of a session's IDs must resolve to the joined trace.

Both files are checked in because Logfire retention expires; re-run this
script only while retention still covers the experiment window.

Auth: set LOGFIRE_READ_TOKEN in .env (create a read token under the Logfire
project settings — the MCP token in .mcp.json is NOT accepted by /v1/query).

Usage:
    uv run python fetch_logfire_exports.py
"""

import argparse
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from recover_wall_clock import (
    EXPERIMENT_LOG,
    GEN_IDS_EXPORT,
    SPANS_EXPORT,
    parse_ts,
)

load_dotenv(Path(__file__).parent / '.env')

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

LOGFIRE_BASE = 'https://logfire-eu.pydantic.dev'


def read_token() -> str:
    token = os.getenv('LOGFIRE_READ_TOKEN', '')
    if not token or token.startswith('your-'):
        raise SystemExit('LOGFIRE_READ_TOKEN not set. Create a read token under the Logfire '
                         'project settings and add it to .env (see .env.example).')
    return token


def query_logfire(sql: str, min_ts: str, max_ts: str, token: str) -> list[dict]:
    """Run a SQL query against the Logfire query API, return rows as dicts."""
    # The API caps responses at 100 rows unless 'limit' is passed explicitly —
    # the SQL LIMIT alone is not enough.
    params = urllib.parse.urlencode({
        'sql': sql, 'min_timestamp': min_ts, 'max_timestamp': max_ts, 'limit': 2000,
    })
    req = urllib.request.Request(
        f'{LOGFIRE_BASE}/v1/query?{params}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            wait = int(e.headers.get('Retry-After') or 0) or 2 ** (attempt + 2)
            print(f'rate limited (429), retrying in {wait}s...')
            time.sleep(wait)
    # Column-oriented: {"columns": [{"name": ..., "values": [...]}, ...]}
    if 'columns' in payload and payload['columns'] and 'values' in payload['columns'][0]:
        cols = {c['name']: c['values'] for c in payload['columns']}
        names = list(cols)
        return [dict(zip(names, vals)) for vals in zip(*cols.values())]
    # Row-oriented fallback: {"rows": [{...}, ...]}
    if 'rows' in payload:
        return payload['rows']
    raise SystemExit(f'Unexpected Logfire response shape: {list(payload)[:5]}')


def query_window(sessions: list[dict]) -> tuple[str, str]:
    lo = min(parse_ts(s['started_at']) for s in sessions) - timedelta(minutes=1)
    hi = max(parse_ts(s['started_at']) for s in sessions) + timedelta(hours=1)
    return lo.isoformat(), hi.isoformat()


def fetch_root_spans(sessions: list[dict], token: str) -> list[list[str]]:
    """[start, end, trace_id] for every root 'orchestrator run' span, chronological."""
    lo, hi = query_window(sessions)
    sql = (
        "SELECT start_timestamp, end_timestamp, trace_id "
        "FROM records "
        "WHERE kind = 'span' AND span_name = 'agent run' "
        "AND message = 'orchestrator run' AND parent_span_id IS NULL "
        "ORDER BY start_timestamp LIMIT 2000"
    )
    rows = query_logfire(sql, lo, hi, token)
    return [[r['start_timestamp'], r['end_timestamp'], r['trace_id']] for r in rows]


# For each experiment generation ID: which trace holds it, and its chronological
# rank among that trace's distinct generations. Each generation emits two span
# flavors (pydantic-ai 'chat …' and the OpenAI client's 'Chat Completion …'),
# hence the GROUP BY dedupe before ranking.
_GEN_RANK_SQL = (
    "WITH gens AS ("
    "SELECT trace_id, attributes->>'gen_ai.response.id' AS gen_id, MIN(start_timestamp) AS ts "
    "FROM records WHERE attributes->>'gen_ai.response.id' IS NOT NULL "
    "GROUP BY trace_id, attributes->>'gen_ai.response.id'), "
    "ranked AS ("
    "SELECT trace_id, gen_id, "
    "ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY ts) AS pos, "
    "COUNT(*) OVER (PARTITION BY trace_id) AS n_gens FROM gens) "
    "SELECT gen_id, trace_id, pos, n_gens FROM ranked WHERE gen_id IN ({ids}) LIMIT {limit}"
)


def fetch_gen_id_ranks(sessions: list[dict], token: str) -> dict[str, dict]:
    """Fetch trace/rank info for every session's first/last generation ID."""
    wanted = sorted(
        {s['first_generation_id'] for s in sessions if s.get('first_generation_id')}
        | {s['last_generation_id'] for s in sessions if s.get('last_generation_id')}
    )
    lo, hi = query_window(sessions)
    ranks: dict[str, dict] = {}
    for i in range(0, len(wanted), 100):  # keep each request's URL well under length limits
        batch = wanted[i:i + 100]
        sql = _GEN_RANK_SQL.format(ids=','.join(f"'{g}'" for g in batch), limit=2 * len(batch))
        for row in query_logfire(sql, lo, hi, token):
            ranks[row['gen_id']] = {
                'trace_id': row['trace_id'], 'pos': int(row['pos']), 'n_gens': int(row['n_gens']),
            }
    missing = [g for g in wanted if g not in ranks]
    if missing:
        print(f'WARNING: {len(missing)} generation IDs not found in Logfire '
              f'(first: {missing[0]}) — verification will fail for their sessions.')
    return ranks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--file', type=Path, default=EXPERIMENT_LOG, help='experiment log to cover')
    parser.add_argument('--spans-out', type=Path, default=SPANS_EXPORT, help='root-span export path')
    parser.add_argument('--gen-ids-out', type=Path, default=GEN_IDS_EXPORT,
                        help='generation-ID export path')
    args = parser.parse_args()

    sessions = [json.loads(line) for line in args.file.open()]
    token = read_token()

    spans = fetch_root_spans(sessions, token)
    with open(args.spans_out, 'w') as f:
        for row in spans:
            f.write(json.dumps(row) + '\n')
    print(f'wrote {len(spans)} root spans to {args.spans_out}')

    ranks = fetch_gen_id_ranks(sessions, token)
    with open(args.gen_ids_out, 'w') as f:
        for gen_id in sorted(ranks):
            r = ranks[gen_id]
            f.write(json.dumps([gen_id, r['trace_id'], r['pos'], r['n_gens']]) + '\n')
    print(f'wrote {len(ranks)} generation-ID ranks to {args.gen_ids_out}')


if __name__ == '__main__':
    main()
