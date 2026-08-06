# Model Pricing and Experiment Costs

## Purpose

Explains where the `cost_usd` values in `experiment_log.jsonl` come from, what
the underlying OpenRouter prices actually are, and what that means for the
cost analysis in the project report.

> **Note:** An earlier mechanism (`sync_pricing.py` writing `model_pricing.json`,
> once described in a separate `sync-pricing.md` doc, both since removed) is gone;
> pricing is fetched live at run time by `model_config.py`.

---

## How costs are calculated at run time

```
run_experiment_matrix.sh
        │  launches per scenario
experiment_runner.py
        │  after all turns: finalize_costs(sessions, OPENROUTER_API_KEY)
experiment_tracker.py  (finalize_costs)
        │  native token counts per generation  ◀── OpenRouter Generation API
        │  price per model                     ◀── model_config.pricing_for()
        │                                          (live catalog fetch,
        │                                           https://openrouter.ai/api/v1/models,
        │                                           cached once per process)
        ▼
cost_usd = input_tokens  × input_rate  / 1e6
         + output_tokens × output_rate / 1e6
         (+ reasoning split / per-request fee, if the model lists them —
            none of the six experiment models did)
        │  rounded to 8 decimals
        ▼
experiment_log.jsonl   (cost_usd per agent run, total_cost_usd per session)
```

Key properties:

- **Native tokens** (what OpenRouter bills on) are the cost basis. Only if
  `usage()` returns nothing does the tracker fall back to normalized tokens and
  set `cost_estimated: true`. This never happened in the final experiment data.
- **The cost is modeled, not billed**: `tokens × advertised list rate`,
  independent of which provider OpenRouter actually routed to and of any
  prompt-cache discount. The real invoice can differ slightly in both
  directions. This is deliberate — it makes costs reproducible from token
  counts and comparable across runs.
- **The rates themselves are not stored in the log.** They existed only in the
  process-wide catalog cache during the run. See "Recovering the rates" below.

---

## What the OpenRouter price actually is

OpenRouter is a router, not (primarily) a host. An open-weight model is served
by many competing providers, each with its own per-token price. The
model-level `pricing` block that `pricing_for()` reads is the **effective rate
of the default endpoint** — in practice the cheapest available provider,
*including* any promotional discount that provider is running.

Consequences observed in this project's data:

| Model type | Price behaviour | In our data |
|---|---|---|
| Proprietary (Opus 4.8, GPT-5.5, Qwen3.7-Max) | Single first-party endpoint, vendor list price, changes only with announced cuts | Constant across the whole experiment window |
| Open-weight (GLM-5.2, DeepSeek-V3.2, Ministral-14B) | Price tracks whichever host is cheapest; moves when providers undercut each other, join, leave, or run promos | GLM-5.2 input price drifted $0.95 → $0.94 → $0.93 per Mtok *during* the run window |

Concrete illustration (checked 2026-07-06): GLM-5.2 had 28 endpoints with input
prices from $0.896 to $3.00 per Mtok — a 3.3× spread for identical weights —
and the advertised catalog price ($0.686) was the cheapest endpoint's base
price with a 36 % promo discount applied. Three days after the experiments
ended, the advertised price was ~27 % below the rate the runs were logged at.
DeepSeek and Ministral are subject to the same mechanism; their prices simply
happened not to change during the window.

---

## Recovering the rates from the log

Because the log stores `cost_usd` and native token counts but not the rates,
the rates can be back-solved exactly: all runs of one model on one day share a
single (input, output) rate pair, so the linear system
`cost = in × a/1e6 + out × b/1e6` over many runs has one exact solution.

```bash
uv run python derive_model_prices.py            # merged date ranges
uv run python derive_model_prices.py --per-day  # one row per day
```

Rates in effect for the final experiment data (all fits exact — every run's
logged cost is reproduced to the cent by its period's rate pair):

| Model | Period | Input $/Mtok | Output $/Mtok |
|---|---|---|---|
| anthropic/claude-opus-4.8 | whole window | 5.00 | 25.00 |
| openai/gpt-5.5 | whole window | 5.00 | 30.00 |
| qwen/qwen3.7-max | whole window | 1.25 | 3.75 |
| deepseek/deepseek-v3.2 | whole window | 0.2288 | 0.3432 |
| mistralai/ministral-14b-2512 | whole window | 0.20 | 0.20 |
| z-ai/glm-5.2 | 2026-06-29 | 0.95 | 3.00 |
| z-ai/glm-5.2 | 2026-06-30 | 0.94 | 3.00 |
| z-ai/glm-5.2 | 2026-07-02 – 07-03 | 0.93 | 3.00 |

---

## Impact on the project analysis

1. **Costs are a snapshot of run-time list prices.** Every `cost_usd` was
   correct at the moment it was logged, but re-deriving costs from today's
   catalog gives different numbers for volatile models (GLM-5.2: ~27 % lower
   as of 2026-07-06). The report should quote the table above as "prices as
   of the experiment window" rather than pointing at live OpenRouter prices.

2. **Cross-model comparisons within the data are sound.** Each model's price
   was constant (or, for GLM-5.2, varied by ≤ 2 %) across the window, so
   per-scenario and per-difficulty cost comparisons between models are not
   distorted by price movements. The only intra-window drift (GLM-5.2 input
   $0.95 → $0.93) is far smaller than the cost differences being compared.

3. **Sensitivity of the headline numbers is small.** Repricing the entire log
   at 2026-07-06 rates changes the grand total by ≈ 0.9 % ($240.40 → $238.27),
   entirely from GLM-5.2. Conclusions about "how much can be saved" are robust
   to this.

4. **Open-weight prices trend down.** The direction of drift (cheaper) means
   cost-savings estimates based on run-time prices are, if anything,
   conservative for the cheap open-weight models — a point worth one sentence
   in the report.

5. **Modeled vs. billed.** Because costs ignore provider routing and prompt-cache
   discounts, the absolute dollar figures are list-price estimates, not invoice
   amounts. State this once in the methodology section; it does not affect
   relative comparisons.

6. **Reproducibility gap (known limitation).** The rates are not persisted per
   run; they are only recoverable because enough runs share each rate pair. If
   the pipeline is used again, consider logging the `ModelPricing` values
   alongside each run in `finalize_costs()`.
