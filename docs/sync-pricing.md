# Pricing Sync

## Purpose

`sync_pricing.py` fetches live model pricing from the OpenRouter API and writes it to `model_pricing.json`. `experiment_tracker.py` reads that file at import time, so cost estimates in `experiment_log.jsonl` always reflect current prices.

Run it once before starting an experiment batch to ensure accurate cost tracking.

---

## Usage

```bash
uv run python sync_pricing.py
```

No API key is required — the OpenRouter `/api/v1/models` endpoint is public.

### Example output

```
Fetching models from https://openrouter.ai/api/v1/models ...
Received 356 models.

  Price changes (3):
    ~ z-ai/glm-5       $0.0000→$0.6000 in  /  $0.0000→$1.9200 out
    ~ deepseek/r1      $0.5500→$0.7000 in  /  $2.1900→$2.5000 out
    ~ openai/gpt-4o    $2.5000→$2.5000 in  /  $9.0000→$10.000 out

Saved 356 models to model_pricing.json

  Spot-check (USD per 1M tokens):
  Model                                            Input    Output
  --------------------------------------------- --------  --------
  z-ai/glm-5                                      0.6000    1.9200
  anthropic/claude-sonnet-4.6                     3.0000   15.0000
  ...
```

---

## How pricing is used

```
sync_pricing.py  ──writes──▶  model_pricing.json
                                      │
                              loaded at import time
                                      │
                              experiment_tracker.py  ──▶  estimate_cost()
                                      │
                              experiment_log.jsonl  (estimated_cost_usd per session)
                                      │
                              analyze_experiments.py  (total_cost_usd in report)
```

`model_pricing.py` is loaded **once per process** by `experiment_tracker.py`. Run `sync_pricing.py` before starting `experiment_runner.py` to pick up any price changes.

---

## model_pricing.json

Generated file committed to the repo so experiment results remain reproducible even after prices change. Format:

```json
{
  "synced_at": "2026-05-19T07:15:00+00:00",
  "source": "https://openrouter.ai/api/v1/models",
  "model_count": 356,
  "models": {
    "anthropic/claude-sonnet-4.6": [3.0, 15.0],
    "z-ai/glm-5": [0.6, 1.92],
    "..."
  }
}
```

Each entry is `[input_usd_per_1m_tokens, output_usd_per_1m_tokens]`.

---

## Fallback behaviour

If `model_pricing.json` does not exist (e.g. on a fresh clone before the first sync), `experiment_tracker.py` falls back to a small hardcoded `_FALLBACK_PRICING` dict covering the most common models. A missing model in both sources results in `estimated_cost_usd = 0.0` for that session.

---

## Recommended workflow

```bash
# 1. Sync prices (once per experiment batch, or whenever you suspect prices changed)
uv run python sync_pricing.py

# 2. Run experiments
uv run python experiment_runner.py --model anthropic/claude-sonnet-4.6 --scenario bgp-01 --file scenarios/bgp-troubleshooting.txt

# 3. Analyse results
uv run python analyze_experiments.py --scenario bgp-01
```