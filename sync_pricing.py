#!/usr/bin/env python3
"""Sync OpenRouter model pricing to model_pricing.json.

Fetches the public OpenRouter /api/v1/models endpoint (no API key required)
and writes all current model prices to model_pricing.json.  experiment_tracker.py
reads that file at import time, so running this script before an experiment batch
ensures cost estimates use accurate, up-to-date prices.

Usage:
    uv run python sync_pricing.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

OPENROUTER_MODELS_URL = 'https://openrouter.ai/api/v1/models'
PRICING_FILE = Path(__file__).parent / 'model_pricing.json'


def _parse_price(value: str | float | None) -> float:
    """Convert an OpenRouter per-token price string to USD per million tokens."""
    if not value:
        return 0.0
    try:
        return round(float(value) * 1_000_000, 6)
    except (ValueError, TypeError):
        return 0.0


def fetch_models() -> list[dict]:
    with httpx.Client(timeout=30) as client:
        resp = client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
    return resp.json().get('data', [])


def build_pricing_table(models: list[dict]) -> dict[str, list[float]]:
    """Return {model_id: [input_per_1m_usd, output_per_1m_usd]} for every model."""
    table = {}
    for m in models:
        model_id = m.get('id')
        if not model_id:
            continue
        pricing = m.get('pricing') or {}
        table[model_id] = [
            _parse_price(pricing.get('prompt')),
            _parse_price(pricing.get('completion')),
        ]
    return table


def load_existing() -> dict[str, list[float]]:
    if not PRICING_FILE.exists():
        return {}
    try:
        return json.loads(PRICING_FILE.read_text()).get('models', {})
    except Exception:
        return {}


def write_pricing_file(table: dict[str, list[float]]) -> None:
    payload = {
        'synced_at': datetime.now(timezone.utc).isoformat(),
        'source': OPENROUTER_MODELS_URL,
        'model_count': len(table),
        'fields': '[input_usd_per_1m_tokens, output_usd_per_1m_tokens]',
        'models': table,
    }
    PRICING_FILE.write_text(json.dumps(payload, indent=2))


def print_diff(old: dict, new: dict) -> None:
    added   = {k for k in new if k not in old}
    removed = {k for k in old if k not in new}
    changed = {
        k for k in new
        if k in old and old[k] != new[k]
    }

    if added:
        print(f'\n  New models ({len(added)}):')
        for k in sorted(added)[:10]:
            print(f'    + {k:<50}  ${new[k][0]:.4f} / ${new[k][1]:.4f}')
        if len(added) > 10:
            print(f'    … and {len(added) - 10} more')

    if removed:
        print(f'\n  Removed models ({len(removed)}):')
        for k in sorted(removed)[:10]:
            print(f'    - {k}')
        if len(removed) > 10:
            print(f'    … and {len(removed) - 10} more')

    if changed:
        print(f'\n  Price changes ({len(changed)}):')
        for k in sorted(changed):
            old_in, old_out = old[k]
            new_in, new_out = new[k]
            print(f'    ~ {k:<50}  ${old_in:.4f}→${new_in:.4f} in  /  ${old_out:.4f}→${new_out:.4f} out')

    if not (added or removed or changed):
        print('  No changes since last sync.')


def main() -> None:
    print(f'Fetching models from {OPENROUTER_MODELS_URL} ...')
    models = fetch_models()
    print(f'Received {len(models)} models.')

    new_table = build_pricing_table(models)
    old_table = load_existing()

    print_diff(old_table, new_table)

    write_pricing_file(new_table)
    print(f'\nSaved {len(new_table)} models to {PRICING_FILE.name}')

    # Spot-check a few well-known models
    spot_check = [
        'z-ai/glm-5',
        'anthropic/claude-sonnet-4.6',
        'anthropic/claude-opus-4.7',
        'google/gemini-2.5-flash',
        'deepseek/deepseek-chat',
    ]
    present = [m for m in spot_check if m in new_table]
    if present:
        print('\n  Spot-check (USD per 1M tokens):')
        print(f'  {"Model":<45} {"Input":>8}  {"Output":>8}')
        print(f'  {"-"*45} {"-"*8}  {"-"*8}')
        for m in present:
            inp, out = new_table[m]
            free = ' (free)' if inp == 0 and out == 0 else ''
            print(f'  {m:<45} {inp:>8.4f}  {out:>8.4f}{free}')


if __name__ == '__main__':
    main()